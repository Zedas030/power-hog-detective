"""Home Assistant access: REST + WebSocket. Read-only by design except notifications and own sensors."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging

import aiohttp

log = logging.getLogger("sfd.ha")

# The only services this tool may ever call. Anything else raises before touching the network.
ALLOWED_SERVICES = {"notify": None, "persistent_notification": {"create", "dismiss"}}


class ReadOnlyViolation(RuntimeError):
    pass


class HAError(RuntimeError):
    pass


class HAClient:
    def __init__(self, url: str, token: str, timeout: float = 20.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._subs: dict[int, callable] = {}

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _sess(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self.session

    async def close(self):
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self.session is not None and not self.session.closed:
            await self.session.close()

    # ------------------------------------------------------------------ REST
    async def get_state(self, entity_id: str) -> dict | None:
        s = await self._sess()
        async with s.get(f"{self.url}/api/states/{entity_id}", headers=self.headers) as r:
            if r.status == 404:
                return None
            if r.status != 200:
                raise HAError(f"GET state {entity_id}: HTTP {r.status}")
            return await r.json()

    async def get_states(self) -> list[dict]:
        s = await self._sess()
        async with s.get(f"{self.url}/api/states", headers=self.headers) as r:
            if r.status != 200:
                raise HAError(f"GET states: HTTP {r.status}")
            return await r.json()

    async def set_state(self, entity_id: str, state, attributes: dict) -> None:
        """Publish one of the tool's own sensors (sensor.<prefix>_*)."""
        if not entity_id.startswith("sensor."):
            raise ReadOnlyViolation(f"Nur eigene Sensoren dürfen gesetzt werden, nicht {entity_id}")
        s = await self._sess()
        async with s.post(f"{self.url}/api/states/{entity_id}", headers=self.headers,
                          data=json.dumps({"state": state, "attributes": attributes})) as r:
            if r.status not in (200, 201):
                raise HAError(f"POST state {entity_id}: HTTP {r.status}")

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        allowed = ALLOWED_SERVICES.get(domain, False)
        if allowed is False or (allowed is not None and service not in allowed):
            raise ReadOnlyViolation(f"Dienst {domain}.{service} ist gesperrt: der Detektiv schaltet nichts.")
        s = await self._sess()
        async with s.post(f"{self.url}/api/services/{domain}/{service}", headers=self.headers,
                          data=json.dumps(data)) as r:
            if r.status not in (200, 201):
                raise HAError(f"{domain}.{service}: HTTP {r.status} {await r.text()}")

    async def save_script(self, script_id: str, config: dict) -> None:
        """Create/update an HA script via the config API (used once for the 'war ok' script)."""
        s = await self._sess()
        async with s.post(f"{self.url}/api/config/script/config/{script_id}", headers=self.headers,
                          data=json.dumps(config)) as r:
            if r.status not in (200, 201):
                raise HAError(f"script {script_id}: HTTP {r.status} {await r.text()}")

    # ------------------------------------------------------------------ WebSocket
    async def ws_connect(self):
        s = await self._sess()
        ws_url = self.url.replace("http", "ws", 1) + "/api/websocket"
        self.ws = await s.ws_connect(ws_url, heartbeat=30, max_msg_size=64 * 1024 * 1024)
        msg = await self.ws.receive_json()
        if msg.get("type") != "auth_required":
            raise HAError(f"Unerwartete Antwort: {msg}")
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        msg = await self.ws.receive_json()
        if msg.get("type") != "auth_ok":
            raise HAError("Anmeldung an Home Assistant fehlgeschlagen (Token prüfen)")
        self._pending.clear()
        self._subs.clear()
        return msg.get("ha_version")

    async def command(self, payload: dict, timeout: float = 120.0):
        if payload.get("type") == "call_service":
            raise ReadOnlyViolation("call_service über WebSocket ist gesperrt")
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send_json({"id": mid, **payload})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    async def subscribe(self, event_type: str, handler) -> int:
        mid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        self._subs[mid] = handler
        await self.ws.send_json({"id": mid, "type": "subscribe_events", "event_type": event_type})
        await asyncio.wait_for(fut, 30)
        return mid

    async def ws_loop(self):
        """Read messages until the connection closes. Dispatches results and events."""
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            data = json.loads(msg.data)
            for item in data if isinstance(data, list) else [data]:
                mid = item.get("id")
                if item.get("type") == "result":
                    fut = self._pending.get(mid)
                    if fut and not fut.done():
                        if item.get("success"):
                            fut.set_result(item.get("result"))
                        else:
                            fut.set_exception(HAError(str(item.get("error"))))
                elif item.get("type") == "event":
                    h = self._subs.get(mid)
                    if h:
                        try:
                            res = h(item["event"])
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception:  # noqa: BLE001 - never let one event kill the loop
                            log.exception("Fehler bei der Verarbeitung eines HA-Ereignisses")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(HAError("WebSocket geschlossen"))

    async def entity_registry(self) -> list[dict]:
        return await self.command({"type": "config/entity_registry/list"})

    async def history(self, entity_ids: list[str], start_iso: str, end_iso: str) -> dict:
        return await self.command({"type": "history/history_during_period", "start_time": start_iso,
                                   "end_time": end_iso, "entity_ids": entity_ids, "minimal_response": True,
                                   "no_attributes": True, "significant_changes_only": False}, timeout=300)

    async def statistics(self, ids: list[str], start_iso: str, end_iso: str, period: str = "hour") -> dict:
        return await self.command({"type": "recorder/statistics_during_period", "start_time": start_iso,
                                   "end_time": end_iso, "statistic_ids": ids, "period": period,
                                   "types": ["mean", "min", "max"]}, timeout=300)
