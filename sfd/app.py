"""The long-running service: HA connection, polling, scheduling, HA sensors, web server."""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta

from . import __version__
from .config import Config
from .detector import HOUSE, HOUSE_ROLES, OPEN_STATES, Detector
from .ha import HAClient
from .notifier import Notifier
from .report import report_title, summary_text, write_report
from .store import Store
from .config import TYPE_EN
from .i18n import L
from .util import Clock

log = logging.getLogger("sfd")

BAD_STATES = ("unavailable", "unknown", "none", "")

_STATE_TEXT = {"unusual": ("unusual", "auffällig"), "learning": ("learning", "lernt"), "ok": ("ok", "ok"),
               "normal": ("normal", "normal"), "no_data": ("no data", "keine Daten"),
               "disconnected": ("disconnected", "keine Verbindung")}


def state_text(code: str) -> str:
    en, de = _STATE_TEXT.get(code, (code, code))
    return L(en, de)


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


class Service:
    def __init__(self, cfg: Config, clock: Clock | None = None):
        self.cfg = cfg
        self.clock = clock or Clock(cfg.tz)
        self.store = Store(cfg.db_path)
        self.ha = HAClient(cfg.ha_url, cfg.ha_token)
        self.notifier = Notifier(cfg, self.store, self.clock, self.ha)
        self.det = Detector(cfg, self.store, self.clock, notify=self.notifier)
        self.entity_map = cfg.entity_map()     # power/house entity -> device
        self.switch_map = cfg.switch_map()     # switch entity -> device
        # sibling entity of the same HA device (e.g. voltage) -> device: proves the plug is alive even when the
        # power value does not change (HA does not refresh last_reported for unchanged values)
        self.alive_map: dict[str, str] = {d.lebenszeichen: k for k, d in cfg.devices.items() if d.lebenszeichen}
        self.stop = asyncio.Event()
        self.started = self.clock.now()
        self.last_daily = self.store.meta("last_daily")
        self.last_report = self.store.meta("last_report")
        self.errors = 0
        self.tasks: list[asyncio.Task] = []
        p = cfg.sensor_prefix
        self.feedback_event = f"{p}_feedback"
        self.script_id = f"{p}_" + L("was_ok", "war_ok")
        adr = (cfg.raw.get("web") or {}).get("adresse") or "localhost"
        self.web_base = f"http://{adr}:{cfg.web_port}"
        # task intervals in seconds (tests shorten them)
        self.check_every = 30.0
        self.deliver_every = 20.0
        self.publish_every = 60.0
        self.schedule_every = 60.0
        self.reconnect_min = 5.0

    # ------------------------------------------------------------------ startup
    def restore_live_state(self):
        """Replay the last 24 h so a run that started before a restart keeps its true duration."""
        now = self.clock.now()
        for key in self.cfg.devices:
            for ts, w in self.store.samples(key, now - 86400, now + 1, with_previous=False):
                self.det.on_sample(key, ts, w, store=False, alive=False)
        last = self.store.last_sample(f"{HOUSE}:hausverbrauch")
        if last:
            self.det.house_live.ts = last[0]
        self.store.set_meta("started", now)

    async def run(self):
        self.restore_live_state()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        from .web import start_web  # local import: aiohttp web only needed for the service
        runner = await start_web(self)
        self.tasks = [asyncio.create_task(c) for c in (
            self.ws_task(), self.poll_task(), self.flush_task(), self.check_task(), self.schedule_task(),
            self.deliver_task(), self.publish_task(), self.registry_task())]
        log.info("%s %s running (web/metrics on port %s, language %s)", self.cfg.app_name, __version__,
                 self.cfg.web_port, self.cfg.language)
        await self.stop.wait()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.flush()
        await runner.cleanup()
        await self.ha.close()
        self.store.close()

    async def _every(self, seconds: float, fn, name: str, first_delay: float = 0):
        await asyncio.sleep(first_delay)
        while not self.stop.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.errors += 1
                log.exception("Error in %s", name)
            await asyncio.sleep(seconds)

    # ------------------------------------------------------------------ HA websocket
    async def ws_task(self):
        backoff = self.reconnect_min
        while not self.stop.is_set():
            reader = None
            try:
                version = await self.ha.ws_connect()
                log.info("Connected to Home Assistant %s", version)
                reader = asyncio.create_task(self.ha.ws_loop())
                await self.ha.subscribe("state_changed", self.on_state_changed)
                await self.ha.subscribe("mobile_app_notification_action", self.on_action)
                await self.ha.subscribe(self.feedback_event, self.on_feedback_event)
                await self.ha.subscribe("homeassistant_started", self.on_ha_started)
                self.det.ha_state(True)
                backoff = self.reconnect_min
                await self.check_registry()   # also finds the liveness siblings
                await self.initial_states()
                self.notify_unsent_findings()
                asyncio.create_task(self.ensure_script())
                await reader
                log.warning("Connection to Home Assistant lost")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Home Assistant unreachable: %s", e)
            finally:
                if reader is not None and not reader.done():
                    reader.cancel()
            self.det.ha_state(False)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def notify_unsent_findings(self):
        """Findings opened while replaying history (pre-learning) have not been sent yet: send them once."""
        for a in self.store.anomalies(("offen",), test=0):
            if a["notified"] is None:
                self.det.notify(f"{self.cfg.app_name}: {self.det.name(a['dev'])}", a["message"], [a["id"]],
                                f"sfd-{a['dev']}")
                self.store.update_anomaly(a["id"], notified=self.clock.now())

    async def on_state_changed(self, event):
        data = event.get("data", {})
        ent = data.get("entity_id")
        new = data.get("new_state")
        if ent in self.entity_map:
            self._apply_state(ent, new, alive=True)
        elif ent in self.alive_map and new:
            if str(new.get("state")).lower() not in BAD_STATES:
                self.det.on_reported(self.alive_map[ent], _ts(new.get("last_reported")) or self.clock.now())
        elif ent in self.switch_map and new:
            self.det.on_switch(self.switch_map[ent], {"on": True, "off": False}.get(new.get("state")))

    def _apply_state(self, ent, st, alive: bool):
        dev = self.entity_map[ent]
        now = self.clock.now()
        lv = self.det.house_live if dev.startswith(HOUSE) else self.det.live.get(dev)
        if st is None:
            if lv is not None:
                lv.entity_missing = True
            return
        if lv is not None and lv.entity_missing:
            lv.entity_missing, lv.missing_reason = False, None
        value = st.get("state")
        reported = _ts(st.get("last_reported")) or _ts(st.get("last_updated"))
        if str(value).lower() in BAD_STATES:
            # HA knows since when the entity is unavailable; survives restarts of the detector
            self.det.on_unavailable(dev, min(now, _ts(st.get("last_changed")) or now), str(value))
            return
        try:
            w = float(value)
        except (TypeError, ValueError):
            return
        unit = (st.get("attributes") or {}).get("unit_of_measurement")
        if unit == "kW":
            w *= 1000
        ts = now if not alive else (_ts(st.get("last_changed")) or now)
        self.det.on_sample(dev, max(ts, now - 5) if alive else now, w, reported=reported, alive=alive)

    async def initial_states(self):
        for ent in list(self.entity_map) + list(self.switch_map) + list(self.alive_map):
            st = await self.ha.get_state(ent)
            if ent in self.entity_map:
                self._apply_state(ent, st, alive=False)
            elif ent in self.switch_map and st:
                self.det.on_switch(self.switch_map[ent], {"on": True, "off": False}.get(st.get("state")))
            elif ent in self.alive_map and st and str(st.get("state")).lower() not in BAD_STATES:
                rep = _ts(st.get("last_reported")) or _ts(st.get("last_updated"))
                if rep:
                    self.det.on_reported(self.alive_map[ent], rep)

    async def on_action(self, event):
        action = (event.get("data") or {}).get("action", "")
        if action.startswith("SFD_TEST_"):
            # button of a [TEST] scenario message: proves the round trip phone -> HA -> detector
            log.info("Test feedback received from the phone: %s", action)
            got = self.store.meta("test_feedback", [])
            self.store.set_meta("test_feedback", (got + [{"ts": self.clock.now(), "action": action}])[-20:])
            self.notifier(f"[TEST] {self.cfg.app_name}: " + L("Got it", "Verstanden"),
                          L(f"Test feedback “Was ok” ({action}) reached the detective.",
                            f"Test-Feedback „War ok“ ({action}) ist beim Detektiv angekommen."), None, "sfd-test")
            return
        if action.startswith("SFD_OK_"):
            done = self.det.feedback(action, source="handy")
            log.info("Feedback from the phone: %s -> %d finding(s)", action, len(done))
            await self.publish()

    async def on_feedback_event(self, event):
        data = event.get("data") or {}
        target = data.get("finding") or data.get("meldung") or data.get("device") or data.get("geraet")
        if not target:
            return
        try:
            done = self.det.feedback(str(target), source="ha", note=data.get("note") or data.get("notiz"))
            log.info("Feedback from HA for %s: %d finding(s)", target, len(done))
        except KeyError as e:
            log.warning("Feedback from HA: %s", e)
        await self.publish()

    async def on_ha_started(self, _event):
        await asyncio.sleep(20)
        await self.publish()

    async def ensure_script(self):
        """Create the "was ok" script once (HA action for feedback). Never overwrites user changes."""
        sid = self.script_id
        try:
            if await self.ha.get_state(f"script.{sid}"):
                return
            names = ", ".join(d.name for d in self.cfg.devices.values())
            dev_f, note_f = L("device", "geraet"), L("note", "notiz")
            await self.ha.save_script(sid, {
                "alias": f"{self.cfg.app_name}: " + L("was ok", "war ok"),
                "description": L(f"Tells the {self.cfg.app_name} that a device's finding was fine. It then learns the "
                                 f"new behaviour.",
                                 f"Meldet dem {self.cfg.app_name}, dass die Auffälligkeit eines Geräts in Ordnung war. "
                                 f"Er lernt das neue Verhalten dann dazu."),
                "icon": "mdi:check-circle-outline",
                "fields": {
                    dev_f: {"name": L("Device", "Gerät"),
                            "description": L(f"Name or key, e.g. {names}, House", f"Name oder Schlüssel, z. B. {names}, Haus"),
                            "required": True, "selector": {"text": {}}},
                    note_f: {"name": L("Note", "Notiz"), "required": False, "selector": {"text": {}}},
                },
                "sequence": [{"event": self.feedback_event,
                              "event_data": {dev_f: "{{ " + dev_f + " }}", note_f: "{{ " + note_f + " | default('') }}"}}],
                "mode": "queued",
            })
            log.info("HA script script.%s created", sid)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not create the feedback script: %s", e)

    # ------------------------------------------------------------------ periodic tasks
    async def poll_task(self):
        async def poll():
            if not self.det.ha_connected:
                return
            for ent in list(self.entity_map):
                st = await self.ha.get_state(ent)
                if ent in self.entity_map:  # may have been renamed meanwhile
                    self._apply_state(ent, st, alive=False)
            for ent, dev in list(self.alive_map.items()):
                st = await self.ha.get_state(ent)
                if st and str(st.get("state")).lower() not in BAD_STATES:
                    rep = _ts(st.get("last_reported")) or _ts(st.get("last_updated"))
                    if rep:
                        self.det.on_reported(dev, rep)
        await self._every(self.cfg.poll_interval, poll, "Abfrage", first_delay=self.cfg.poll_interval)

    async def flush_task(self):
        async def flush():
            self.store.flush()
        await self._every(self.cfg.flush_interval, flush, "Schreiben")

    async def check_task(self):
        async def check():
            self.det.live_check()
            self.det.ha_down_check()
        await self._every(self.check_every, check, "live check", first_delay=self.check_every * 3)

    async def deliver_task(self):
        async def deliver():
            if self.det.ha_connected:
                await self.notifier.deliver()
        await self._every(self.deliver_every, deliver, "Zustellung", first_delay=min(5, self.deliver_every))

    async def publish_task(self):
        await self._every(self.publish_every, self.publish, "HA-Sensoren", first_delay=min(20, self.publish_every))

    async def registry_task(self):
        await self._every(self.cfg.registry_interval, self.check_registry, "entity check",
                          first_delay=self.cfg.registry_interval)

    async def schedule_task(self):
        await self._every(self.schedule_every, self.scheduled, "Zeitplan", first_delay=min(15, self.schedule_every))

    async def scheduled(self):
        now = self.clock.local()
        yesterday = (now - timedelta(days=1)).date()
        # daily job after 00:20 for the finished day (catches up after downtime)
        if (now.hour, now.minute) >= (0, 20) and self.last_daily != yesterday.isoformat():
            await self.daily_job(yesterday)
        # monthly report
        y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        key = f"{y}-{m:02d}"
        if (now.day >= self.cfg.report_day and (now.hour, now.minute) >= self.cfg.report_time
                and self.last_report != key):
            await self.monthly_report(y, m)

    async def daily_job(self, day):
        log.info("Daily evaluation for %s", day)
        start = self.store.meta("last_daily")
        days = [day]
        if start:
            d = datetime.fromisoformat(start).date() + timedelta(days=1)
            days = []
            while d <= day and len(days) < 7:  # catch up after a longer outage
                days.append(d)
                d += timedelta(days=1)
        for d in days:
            await asyncio.to_thread(self.det.process_day, d)
            await asyncio.to_thread(self.det.evaluate_day, d)
        if self.det.ha_connected:
            try:  # hourly statistics from HA: solar feed-in and gap filling for the reports
                from .history import import_statistics
                n = await import_statistics(self.cfg, self.store, self.clock, self.ha, since=day - timedelta(days=3))
                log.info("HA statistics: %d hourly values imported", n)
            except Exception as e:  # noqa: BLE001
                log.warning("HA statistics not imported: %s", e)
        removed = await asyncio.to_thread(self.store.compact, self.clock.now(), self.cfg.raw_days,
                                          self.cfg.minute_days, self.cfg.quarter_days)
        log.info("Compaction: %s", removed)
        self.last_daily = day.isoformat()
        self.store.set_meta("last_daily", self.last_daily)
        await self.publish()

    async def monthly_report(self, y, m):
        path, rep = await asyncio.to_thread(write_report, self.cfg, self.store, self.det, self.clock, y, m)
        log.info("Monthly report written: %s", path)
        self.notifier(report_title(self.cfg, rep), summary_text(rep) + L(" Full report: ", " Ganzer Bericht: ") +
                      f"{self.web_base}/berichte/{path.name}", None, "sfd-bericht")
        self.last_report = f"{y}-{m:02d}"
        self.store.set_meta("last_report", self.last_report)

    async def check_registry(self):
        if not self.det.ha_connected:
            return
        reg = await self.ha.entity_registry()
        by_id = {e["entity_id"]: e for e in reg}
        by_uid = {(e.get("platform"), e.get("unique_id")): e["entity_id"] for e in reg}
        self._find_siblings(reg, by_id)
        watched = dict(self.entity_map)
        watched.update(self.switch_map)
        for ent, dev in list(watched.items()):
            e = by_id.get(ent)
            name = self.det.name(dev.split(":")[0]) if not dev.startswith(HOUSE) else "Haus"
            if e:
                if not self.store.entity_unique(ent):
                    self.store.set_entity(ent, e.get("unique_id"), e.get("platform"))
                if dev.startswith(HOUSE) and e.get("disabled_by"):
                    self.det.house_live.entity_missing = True
                    self.det.house_live.missing_reason = (f"{ent} ist in Home Assistant deaktiviert "
                                                          f"(Integration {e.get('platform')})")
                continue
            known = self.store.entity_unique(ent)
            new_id = by_uid.get((known[1], known[0])) if known else None
            metric = f"entitaet:{ent}"
            if self.store.find_open(dev.split(":")[0] if not dev.startswith(HOUSE) else HOUSE, metric):
                continue
            target = HOUSE if dev.startswith(HOUSE) else dev
            if new_id:
                msg = L(f"{name}: entity renamed – {ent} is now {new_id}. I follow it automatically; please update "
                        f"config.yaml.",
                        f"{name}: Entität umbenannt – {ent} heißt jetzt {new_id}. Ich verfolge sie automatisch "
                        f"weiter; bitte in config.yaml nachziehen.")
                self._follow(ent, new_id)
                self.store.set_entity(new_id, known[0], known[1])
            else:
                msg = L(f"{name}: the entity {ent} no longer exists in Home Assistant – device removed or renamed? "
                        f"Please check config.yaml.",
                        f"{name}: Die Entität {ent} gibt es in Home Assistant nicht mehr – Gerät entfernt oder "
                        f"umbenannt? Bitte config.yaml prüfen.")
                lv = self.det.house_live if dev.startswith(HOUSE) else self.det.live.get(dev)
                if lv is not None:
                    lv.entity_missing = True
                    lv.missing_reason = L(f"the entity {ent} is missing", f"die Entität {ent} fehlt")
            self.det._open(target, metric, "system", msg)

    def _find_siblings(self, reg, by_id):
        configured = {d.lebenszeichen for d in self.cfg.devices.values() if d.lebenszeichen}
        known = {k for k in self.alive_map.values()}
        for key, d in self.cfg.devices.items():
            if not d.aktiv or d.lebenszeichen or key in known:
                continue
            e = by_id.get(d.leistung)
            if not e or not e.get("device_id"):
                continue
            sib = [x["entity_id"] for x in reg if x.get("device_id") == e["device_id"]
                   and x["entity_id"].startswith("sensor.") and x["entity_id"] != d.leistung
                   and not x.get("disabled_by") and x["entity_id"] not in configured]
            sib.sort(key=lambda x: (0 if any(k in x for k in ("spannung", "voltage")) else
                                    1 if any(k in x for k in ("stromstarke", "current")) else 2, x))
            if sib:
                self.alive_map[sib[0]] = key
                log.info("Liveness sensor for %s: %s", d.name, sib[0])

    def _follow(self, old, new):
        if old in self.entity_map:
            dev = self.entity_map.pop(old)
            self.entity_map[new] = dev
            if not dev.startswith(HOUSE):
                self.cfg.devices[dev].leistung = new
            else:
                setattr(self.cfg.house, dev.split(":")[1], new)
        if old in self.switch_map:
            dev = self.switch_map.pop(old)
            self.switch_map[new] = dev
            self.cfg.devices[dev].schalter = new

    # ------------------------------------------------------------------ HA sensors
    def status(self) -> dict:
        open_ = self.store.anomalies(OPEN_STATES, test=0)
        learning = {k: self.det.learning_progress(k) for k, _ in self.det.targets()}
        if any(a["state"] == "offen" for a in open_):
            state = "unusual"
        elif any(n < m for n, m in learning.values()):
            state = "learning"
        else:
            state = "ok"
        if not self.det.ha_connected:
            state = "disconnected"
        return {"state": state, "open": open_, "learning": learning}

    async def publish(self):
        if not self.det.ha_connected:
            return
        st = self.status()
        p = self.cfg.sensor_prefix
        open_ = [a for a in st["open"] if a["state"] == "offen"]
        await self.ha.set_state(f"sensor.{p}_status", state_text(st["state"]), {
            "friendly_name": f"{self.cfg.app_name} Status", "icon": "mdi:magnify-scan",
            L("open_findings", "offene_meldungen"): [a["message"] for a in open_][:10], "version": __version__,
            L("web_ui", "weboberflaeche"): f"{self.web_base}/"})
        await self.ha.set_state(f"sensor.{p}_" + L("open_findings", "offene_auffaelligkeiten"), len(open_), {
            "friendly_name": f"{self.cfg.short_name} " + L("open findings", "offene Auffälligkeiten"),
            "icon": "mdi:alert-circle-outline", "state_class": "measurement"})
        yesterday = (self.clock.local().date() - timedelta(days=1)).isoformat()
        hd = self.store.daily_day(HOUSE, yesterday)
        house_metrics = (("nacht_w", L("night_base_load", "nachtgrundlast"),
                          L("night base load", "Nächtliche Grundlast")),
                         ("unzugeordnet_nacht_w", L("unassigned_base_load", "unzugeordnete_grundlast"),
                          L("base load without metering plug", "Grundlast ohne Messsteckdose")))
        for metric, sid, label in (house_metrics if self.cfg.house.hausverbrauch else ()):
            await self.ha.set_state(f"sensor.{p}_{sid}",
                                    hd.get(metric, "unknown"), {
                                        "friendly_name": f"{self.cfg.short_name} {label}", "unit_of_measurement": "W",
                                        "device_class": "power", "state_class": "measurement", "tag": yesterday})
        for key, typ in self.det.targets():
            if key == HOUSE:
                continue
            d = self.cfg.devices[key]
            n, mx = st["learning"][key]
            mine = [a for a in open_ if a["dev"] == key]
            lv = self.det.live[key]
            state = "unusual" if mine else ("learning" if n < mx else "normal")
            if lv.entity_missing or lv.state != "ok":
                state = "no_data"
            attrs = {"friendly_name": f"{self.cfg.short_name} {d.name}", "icon": "mdi:power-plug-outline",
                     L("device_type", "geraetetyp"): TYPE_EN.get(typ, typ) if self.cfg.language == "en" else typ,
                     L("learning_days", "lerntage"): f"{n}/{mx}",
                     L("finding", "meldung"): mine[0]["message"] if mine else None,
                     L("yesterday", "gestern"): self.store.daily_day(key, yesterday)}
            state = state_text(state)
            await self.ha.set_state(f"sensor.{p}_{key}", state, attrs)
