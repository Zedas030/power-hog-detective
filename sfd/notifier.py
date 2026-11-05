"""Queue notifications in SQLite and deliver them through a Home Assistant notify service."""
from __future__ import annotations

import logging

from .config import Config
from .ha import HAClient
from .store import Store
from .i18n import L
from .util import Clock

log = logging.getLogger("sfd.notify")


class Notifier:
    def __init__(self, cfg: Config, store: Store, clock: Clock, ha: HAClient | None = None):
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self.ha = ha
        self.sent = 0
        self.failed = 0

    def __call__(self, title: str, message: str, action: str | None, tag: str | None):
        data = {"group": self.cfg.sensor_prefix, "tag": tag or self.cfg.sensor_prefix}
        if action:
            data["actions"] = [{"action": action, "title": L("Was ok", "War ok")}]
        self.store.enqueue(self.clock.now(), title, message, data)

    async def deliver(self) -> int:
        """Send queued messages. Stops at the first failure (HA down) and retries later."""
        if self.ha is None:
            return 0
        n = 0
        domain, service = self.cfg.notify.split(".", 1)
        for oid, ts, title, message, data, attempts in self.store.pending():
            payload = {"title": title, "message": message}
            if domain == "notify" and service.startswith("mobile_app"):
                payload["data"] = data
            try:
                await self.ha.call_service(domain, service, payload)
                if self.cfg.persistent_too:
                    await self.ha.call_service("persistent_notification", "create",
                                               {"title": title, "message": message,
                                                "notification_id": data.get("tag", self.cfg.sensor_prefix)})
            except Exception as e:  # noqa: BLE001
                self.store.mark_attempt(oid)
                self.failed += 1
                log.warning("Notification %s not delivered (%s), retrying later", oid, e)
                break
            self.store.mark_sent(oid, self.clock.now())
            self.sent += 1
            n += 1
        return n
