"""A small fake Home Assistant (REST + WebSocket) for the end-to-end test."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from aiohttp import WSMsgType, web

TOKEN = "test-token"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class FakeHA:
    def __init__(self):
        self.states: dict[str, dict] = {}
        self.registry: list[dict] = []
        self.service_calls: list[tuple[str, str, dict]] = []
        self.posted_states: dict[str, dict] = {}
        self.scripts: dict[str, dict] = {}
        self.clients: list[tuple[web.WebSocketResponse, dict]] = []
        self.runner = None
        self.port = None
        self.accept = True
        self.frozen: set[str] = set()  # entities whose last_reported does not advance (plug silent)

    # ---------------------------------------------------------------- state helpers
    def set(self, entity_id, state, unit=None, reported: float | None = None, unique_id=None, platform="tplink",
            device_id=None):
        now = datetime.now(timezone.utc).timestamp()
        rep = reported if reported is not None else now
        if reported is not None:
            self.frozen.add(entity_id)
        else:
            self.frozen.discard(entity_id)
        self.states[entity_id] = {
            "entity_id": entity_id, "state": str(state),
            "attributes": {"unit_of_measurement": unit, "friendly_name": entity_id} if unit else {},
            "last_changed": iso(rep), "last_updated": iso(rep), "last_reported": iso(rep)}
        if not any(e["entity_id"] == entity_id for e in self.registry):
            self.registry.append({"entity_id": entity_id, "unique_id": unique_id or f"uid-{entity_id}",
                                  "platform": platform, "disabled_by": None, "device_id": device_id})
        return self.states[entity_id]

    def rename(self, old, new):
        st = self.states.pop(old)
        st["entity_id"] = new
        self.states[new] = st
        for e in self.registry:
            if e["entity_id"] == old:
                e["entity_id"] = new

    async def change(self, entity_id, state, unit="W"):
        old = self.states.get(entity_id)
        new = self.set(entity_id, state, unit)
        await self.fire("state_changed", {"entity_id": entity_id, "old_state": old, "new_state": new})

    async def fire(self, event_type, data):
        for ws, subs in list(self.clients):
            for sid, et in subs.items():
                if et == event_type and not ws.closed:
                    await ws.send_json({"id": sid, "type": "event",
                                        "event": {"event_type": event_type, "data": data}})

    async def drop_clients(self):
        for ws, _ in list(self.clients):
            await ws.close()
        self.clients.clear()

    def notifications(self):
        return [d for dom, svc, d in self.service_calls if dom == "notify"]

    # ---------------------------------------------------------------- HTTP handlers
    def _auth(self, req):
        if req.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()

    async def get_state(self, req):
        self._auth(req)
        eid = req.match_info["eid"]
        st = self.states.get(eid)
        if not st:
            raise web.HTTPNotFound()
        if eid not in self.frozen:  # a healthy integration keeps reporting even if the value is unchanged
            st["last_reported"] = iso(datetime.now(timezone.utc).timestamp())
        return web.json_response(st)

    async def get_states(self, req):
        self._auth(req)
        return web.json_response(list(self.states.values()))

    async def post_state(self, req):
        self._auth(req)
        data = await req.json()
        self.posted_states[req.match_info["eid"]] = data
        return web.json_response(data, status=200)

    async def service(self, req):
        self._auth(req)
        self.service_calls.append((req.match_info["dom"], req.match_info["svc"], await req.json()))
        return web.json_response([])

    async def script(self, req):
        self._auth(req)
        cfg = await req.json()
        self.scripts[req.match_info["sid"]] = cfg
        self.set(f"script.{req.match_info['sid']}", "off")
        return web.json_response({"result": "ok"})

    async def websocket(self, req):
        if not self.accept:
            raise web.HTTPServiceUnavailable()
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        await ws.send_json({"type": "auth_required", "ha_version": "2099.1.0"})
        msg = await ws.receive_json()
        if msg.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2099.1.0"})
        subs: dict[int, str] = {}
        self.clients.append((ws, subs))
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                break
            data = json.loads(m.data)
            t, mid = data.get("type"), data.get("id")
            if t == "subscribe_events":
                subs[mid] = data["event_type"]
                await ws.send_json({"id": mid, "type": "result", "success": True, "result": None})
            elif t == "config/entity_registry/list":
                await ws.send_json({"id": mid, "type": "result", "success": True, "result": self.registry})
            else:
                await ws.send_json({"id": mid, "type": "result", "success": False,
                                    "error": {"code": "unknown_command", "message": t}})
        return ws

    async def start(self):
        app = web.Application()
        app.router.add_get("/api/states/{eid}", self.get_state)
        app.router.add_get("/api/states", self.get_states)
        app.router.add_post("/api/states/{eid}", self.post_state)
        app.router.add_post("/api/services/{dom}/{svc}", self.service)
        app.router.add_post("/api/config/script/config/{sid}", self.script)
        app.router.add_get("/api/websocket", self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self):
        await self.drop_clients()
        await self.runner.cleanup()


async def wait_for(pred, timeout=40.0, step=0.1):
    t = 0.0
    while t < timeout:
        r = pred()
        if r:
            return r
        await asyncio.sleep(step)
        t += step
    raise AssertionError("Bedingung nicht erfüllt innerhalb von %.0f s" % timeout)
