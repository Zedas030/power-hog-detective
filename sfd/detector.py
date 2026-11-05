"""Detection engine: daily feature processing, baseline evaluation, programs, live checks, feedback."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta

from . import explain
from .baseline import evaluate
from .config import Config, Device
from .features import (EVAL_METRICS, MIN_COVERAGE, PRIORITY, device_day, house_day, learned_runtime_limit,
                       learned_zero_limit)
from .segment import Run, buckets, energy_wh, hourly_energy, steps
from .store import Store
from .util import Clock, median

log = logging.getLogger("sfd.detector")

HOUSE = "_haus"
HOUSE_ROLES = ("hausverbrauch", "netzbezug", "einspeisung")
OPEN_STATES = ("offen", "stumm")

# directions that are harmless and never alarm (e.g. less consumption)
BENIGN = {("pause_min", 1), ("zyklen", -1), ("laufanteil", -1), ("kwh", -1), ("standby_w", -1),
          ("nacht_w", -1), ("unzugeordnet_nacht_w", -1)}


@dataclass
class LiveDev:
    w: float | None = None
    ts: float | None = None          # time of last received value
    reported: float | None = None    # HA last_reported (proves the plug still talks even if unchanged)
    state: str = "ok"                # 'ok' or the HA state string when unavailable/unknown
    unavail_since: float | None = None
    switch_on: bool | None = None
    run_start: float | None = None
    last_on: float | None = None
    zero_since: float | None = None
    entity_missing: bool = False
    alive: float | None = None       # last proof that HA receives fresh values for this entity
    missing_reason: str | None = None


class Detector:
    def __init__(self, cfg: Config, store: Store, clock: Clock, notify=None, test: bool = False):
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self._notify = notify  # callable(title, message, action_id|None, tag)
        self.test = 1 if test else 0
        self.live: dict[str, LiveDev] = {k: LiveDev() for k in cfg.devices}
        self.house_live = LiveDev()
        self.ha_connected = False
        self.ha_down_since: float | None = None
        self.notifications = 0
        self.silent = False  # True while replaying history: findings are stored but not sent

    # ------------------------------------------------------------------ helpers
    def name(self, dev: str) -> str:
        if dev == HOUSE:
            return "Haus"
        d = self.cfg.devices.get(dev)
        return d.name if d else dev

    def profile(self, dev: str) -> str:
        d = self.cfg.devices.get(dev)
        return explain.profile_for(d.name, d.profil) if d else "allgemein"

    def notify(self, title: str, message: str, ids=None, tag: str | None = None):
        if self.silent:
            return
        if self.test:
            title = "[TEST] " + title
        prefix = "SFD_TEST_" if self.test else "SFD_OK_"
        action = (prefix + "_".join(str(i) for i in ids)) if ids else None
        self.notifications += 1
        log.info("Meldung: %s – %s", title, message)
        if self._notify:
            self._notify(title, message, action, tag)

    def _open(self, dev, metric, kind, message, value=None, baseline=None, direction=0, start_day=None,
              details=None, grp=None, notify=True, tag=None) -> int:
        now = self.clock.now()
        aid = self.store.open_anomaly(dev=dev, metric=metric, kind=kind, direction=direction,
                                      start_day=(start_day or self.clock.day_of(now)).isoformat(), opened=now,
                                      message=message, value=value, baseline=baseline, details=details, grp=grp,
                                      test=self.test)
        if notify:
            self.notify(f"Stromfresser-Detektiv: {self.name(dev)}", message, [aid], tag or f"sfd-{dev}")
            self.store.update_anomaly(aid, notified=now)
        return aid

    def _close(self, a, state: str, recovery_text: str | None = None):
        self.store.update_anomaly(a["id"], state=state, closed=self.clock.now())
        if recovery_text and a["state"] == "offen" and a.get("notified"):
            self.notify(f"Stromfresser-Detektiv: {self.name(a['dev'])}", recovery_text, None, f"sfd-{a['dev']}")

    # ------------------------------------------------------------------ daily processing
    def process_day(self, day: date, data_end: float | None = None, house: bool = True,
                    aggregates: bool = True, devices: bool = True) -> dict:
        """Compute features, runs, hourly energy and aggregates of one finished local day."""
        ds, de = self.clock.day_bounds(day)
        data_end = data_end if data_end is not None else self.clock.now()
        out = {}
        for key, dev in self.cfg.devices.items():
            if not dev.aktiv or not devices:
                continue
            samples = self.store.samples(key, ds - 12 * 3600, min(de + 12 * 3600, data_end))
            m, day_runs = device_day(dev, samples, ds, de, data_end)
            self.store.put_daily(key, day.isoformat(), m)
            out[key] = m
            st = steps(samples, ds, de)
            if st:
                self.store.put_energy(key, hourly_energy(st, ds, de), "messung")
                if aggregates:
                    self.store.put_aggregates("minute", key, [(b, a, mx) for b, a, mx, _ in buckets(st, ds, de, 60)])
                    self.store.put_aggregates("quarter", key, [(b, a, mx) for b, a, mx, _ in buckets(st, ds, de, 900)])
            if dev.typ == "programm" and dev.programme_pruefen:
                self._programs(dev, [r for r in day_runs if not r.open])
            self.store.put_runs(key, day_runs)
            self._learn_limits(dev, day)
        if house and self.cfg.house.hausverbrauch:
            out[HOUSE] = self._process_house(day, ds, de, data_end, aggregates)
        return out

    def _process_house(self, day, ds, de, data_end, aggregates):
        h = self.cfg.house
        s = {role: self.store.samples(f"{HOUSE}:{role}", ds, min(de, data_end)) for role in HOUSE_ROLES
             if getattr(h, role)}
        plug = {k: self.store.samples(k, ds, min(de, data_end)) for k, d in self.cfg.devices.items() if d.aktiv}
        m = house_day(s.get("hausverbrauch"), s.get("netzbezug"), s.get("einspeisung"), plug, ds, de,
                      h.nacht_von, h.nacht_bis, h.dle_schwelle_w if h.dle_aktiv else None)
        if m.get("kwh") is not None:
            # plausibility: the whole house cannot use less than the metered plugs
            plugs_kwh = sum(energy_wh(steps(p, ds, de, 600)) for p in plug.values()) / 1000
            if plugs_kwh > 0.2 and m["kwh"] < 0.9 * plugs_kwh * m.get("coverage", 1):
                if not self.store.find_open(HOUSE, "hausdaten_unplausibel", self.test):
                    self._open(HOUSE, "hausdaten_unplausibel", "system",
                               f"Hausdaten unplausibel: {self.cfg.house.hausverbrauch} meldet für den "
                               f"{day.strftime('%d.%m.')} {m['kwh']:.1f} kWh, die Messsteckdosen allein "
                               f"verbrauchten {plugs_kwh:.1f} kWh. Die Hausauswertung (Grundlast, Solaranteil) "
                               f"ruht, bis der Sensor echte Werte liefert – Sensor in config.yaml prüfen.",
                               start_day=day)
                self.store.put_daily(HOUSE, day.isoformat(), {"coverage": 0, "unplausibel": 1,
                                                              "kwh_gemeldet": m["kwh"], "steckdosen_kwh": plugs_kwh})
                return {"unplausibel": 1}
            a = self.store.find_open(HOUSE, "hausdaten_unplausibel", self.test)
            if a:
                self._close(a, "erledigt", "Hausdaten sind wieder plausibel.")
        if m:
            old = self.store.daily_day(HOUSE, day.isoformat())
            if m.get("coverage", 0) >= old.get("coverage", 0):
                self.store.put_daily(HOUSE, day.isoformat(), m)
        for role, samples in s.items():
            st = steps(samples, ds, de, 900)
            if st:
                self.store.put_energy(f"{HOUSE}:{role}", hourly_energy(st, ds, de), "messung")
                if aggregates:
                    self.store.put_aggregates("quarter", f"{HOUSE}:{role}",
                                              [(b, a, mx) for b, a, mx, _ in buckets(st, ds, de, 900)])
        return m

    def _learn_limits(self, dev: Device, day: date):
        ds, de = self.clock.day_bounds(day)
        start = ds - (self.cfg.window_days - 1) * 86400
        rows = self.store.runs(dev.key, start, de)
        minutes = [(e - s) / 60 for s, e, *_ in rows]
        if dev.auto_laufzeit:
            lim = learned_runtime_limit(minutes)
            if lim:
                self.store.set_override(dev.key, "auto_laufzeit", lim)
        if dev.typ == "zyklus":
            pauses = [(b[0] - a[1]) / 60 for a, b in zip(rows, rows[1:]) if b[0] - a[1] < 86400]
            lim = learned_zero_limit(pauses)
            if lim:
                self.store.set_override(dev.key, "auto_null", lim)

    # ------------------------------------------------------------------ programs
    def _programs(self, dev: Device, runs: list[Run]):
        progs = self.store.programs(dev.key)
        thr = self.cfg.program_deviation
        for r in sorted(runs, key=lambda r: r.start):
            d_min, kwh = max(r.minutes, 0.1), max(r.wh / 1000, 0.001)
            best, best_dist = None, None
            for pid, p in progs.items():
                md = median([x[0] for x in p["recent"]])
                me = median([x[1] for x in p["recent"]])
                dist = max(abs(math.log(d_min / md)), abs(math.log(kwh / me)))
                if best_dist is None or dist < best_dist:
                    best, best_dist = pid, dist
            if best is None or best_dist > math.log(1.6):
                pid = chr(ord("A") + len(progs)) if len(progs) < 26 else f"P{len(progs) + 1}"
                progs[pid] = {"name": f"Programm {pid}", "recent": [[round(d_min, 1), round(kwh, 3)]]}
                r.program = pid
                self.store.put_program(dev.key, pid, progs[pid]["name"], progs[pid]["recent"])
                continue
            p = progs[best]
            r.program = best
            md = median([x[0] for x in p["recent"]])
            me = median([x[1] for x in p["recent"]])
            flag = None
            if len(p["recent"]) >= 3:
                if kwh / me - 1 > thr:
                    flag = "energie"
                elif d_min / md - 1 > thr:
                    flag = "dauer"
            r.flag = flag
            if not flag:
                p["recent"] = (p["recent"] + [[round(d_min, 1), round(kwh, 3)]])[-30:]
                self.store.put_program(dev.key, best, p["name"], p["recent"])
                for kind in ("dauer", "energie"):
                    a = self.store.find_open(dev.key, f"programm:{best}:{kind}", self.test)
                    if a:
                        self._close(a, "erledigt", f"{dev.name}: {p['name']} läuft wieder normal.")
                continue
            # stable deviation: N of the last 3 runs of this program flagged the same way
            prev = [row for row in self.store.runs(dev.key, 0, r.start) if row[4] == best][-2:]
            flags = [row[5] for row in prev] + [flag]
            n_flag = sum(1 for f in flags[-3:] if f == flag)
            metric = f"programm:{best}:{flag}"
            if n_flag >= self.cfg.program_stable_runs and not self.store.find_open(dev.key, metric, self.test):
                cur, base = (kwh, me) if flag == "energie" else (d_min, md)
                msg = explain.program_message(dev.name, self.profile(dev.key), p["name"], flag, cur, base, n_flag)
                self._open(dev.key, metric, "programm", msg, value=cur, baseline=base, direction=1,
                           start_day=self.clock.day_of(r.start), details={"programm": best})

    # ------------------------------------------------------------------ daily evaluation
    def targets(self):
        for key, d in self.cfg.devices.items():
            if d.aktiv:
                yield key, d.typ
        if self.cfg.house.hausverbrauch:
            yield HOUSE, "haus"

    def evaluate_day(self, day: date, notify: bool = True, only=None) -> list[int]:
        """Compare the finished day with the learned baseline; open/close anomalies. Returns new ids."""
        new_ids = []
        dstr = day.isoformat()
        for key, typ in self.targets():
            if only is not None and key not in only:
                continue
            today = self.store.daily_day(key, dstr)
            if today.get("coverage", 0) < (0.5 if key == HOUSE else MIN_COVERAGE):
                continue
            triggered = []
            results = {}
            for metric, spec in EVAL_METRICS[typ].items():
                x = today.get(metric)
                if x is None:
                    continue
                res = self._eval_metric(key, metric, spec, day, x)
                results[metric] = res
                deviating = res.deviating and (metric, res.direction) not in BENIGN
                self.store.set_flag(key, metric, dstr, res.direction if deviating else 0)
                a = self.store.find_open(key, metric, self.test)
                if res.status == "lernt":
                    continue
                if deviating:
                    n, start = self._streak(key, metric, day, res.direction)
                    if a:
                        self.store.update_anomaly(a["id"], value=res.value)
                        a_start = date.fromisoformat(a["start_day"])
                        if a["kind"] == "taeglich" and (day - a_start).days >= self.cfg.max_excluded_days:
                            self._relearn(key, metric, a_start, "automatisch")
                            self.store.update_anomaly(a["id"], state="gelernt", closed=self.clock.now())
                            if notify:
                                self.notify(f"Stromfresser-Detektiv: {self.name(key)}",
                                            explain.learned_message(self.name(key), metric, (day - a_start).days + 1),
                                            None, f"sfd-{key}")
                    elif n >= self.cfg.stable_days:
                        triggered.append((metric, res, n, start))
                elif a and a["kind"] == "taeglich":
                    if self._normal_streak(key, metric, day) >= self.cfg.normal_days_to_clear:
                        self._close(a, "erledigt", explain.recovery_message(self.name(key), metric)
                                    if (notify and self.cfg.notify_recovery) else None)
            if triggered:
                new_ids += self._open_group(key, typ, day, triggered, results, notify)
        return new_ids

    def _open_group(self, key, typ, day, triggered, results, notify):
        triggered.sort(key=lambda t: PRIORITY.index(t[0]) if t[0] in PRIORITY else 99)
        texts, ids = [], []
        grp = f"{key}-{day.isoformat()}"
        for i, (metric, res, n, start) in enumerate(triggered):
            extra = {}
            if metric == "nacht_w" and "unzugeordnet_nacht_w" in results:
                u = results["unzugeordnet_nacht_w"]
                if u.baseline is not None:
                    extra["unzugeordnet_diff"] = u.value - u.baseline
            text = explain.daily_message(self.name(key) if key != HOUSE else "Haus", typ, self.profile(key), metric,
                                         res.direction, res.value, res.baseline, n, start, self.cfg.price_import,
                                         extra)
            texts.append(text)
            ids.append(self._open(key, metric, "taeglich", text, value=res.value, baseline=res.baseline,
                                  direction=res.direction, start_day=start, grp=grp, notify=False,
                                  details={"tage": n, "rel": res.rel, "z": res.z}))
        message = texts[0]
        if len(texts) > 1:
            extras = [explain.METRIC_LABEL.get(t[0], t[0]) for t in triggered[1:]]
            message += " Ebenfalls auffällig: " + ", ".join(extras) + "."
        if notify:
            self.notify(f"Stromfresser-Detektiv: {self.name(key)}", message, ids, f"sfd-{key}")
            for aid in ids:
                self.store.update_anomaly(aid, notified=self.clock.now())
        return ids

    def _eval_metric(self, key, metric, spec, day: date, x: float):
        min_abs, rel_thr, use_rel, circular = spec
        bstart, reason = self.store.baseline_start(key, metric)
        first = day - timedelta(days=self.cfg.window_days)
        if bstart and date.fromisoformat(bstart) > first:
            first = date.fromisoformat(bstart)
        last = day - timedelta(days=1)
        hist = self.store.daily(key, metric, first.isoformat(), last.isoformat())
        cov = dict(self.store.daily(key, "coverage", first.isoformat(), last.isoformat()))
        flags = self.store.flags(key, metric, first.isoformat(), last.isoformat())
        min_cov = 0.5 if key == HOUSE else MIN_COVERAGE
        values = [v for d, v in hist if cov.get(d, 0) >= min_cov and not flags.get(d)]
        min_n = self.cfg.min_days_after_feedback if bstart else self.cfg.min_days
        return evaluate(values, x, min_abs=min_abs, rel_threshold=rel_thr or self.cfg.rel_threshold,
                        use_rel=use_rel, circular=circular, z_threshold=self.cfg.z_threshold, min_n=min_n)

    def _streak(self, key, metric, day: date, direction: int):
        first = day - timedelta(days=60)
        flags = self.store.flags(key, metric, first.isoformat(), day.isoformat())
        have = {d for d, _ in self.store.daily(key, metric, first.isoformat(), day.isoformat())}
        n, start, skipped, d = 0, day, 0, day
        while d >= first:
            ds = d.isoformat()
            if ds not in have:
                skipped += 1
                if skipped > 2:
                    break
            elif flags.get(ds) == direction:
                n += 1
                start = d
                skipped = 0
            else:
                break
            d -= timedelta(days=1)
        return n, start

    def _normal_streak(self, key, metric, day: date) -> int:
        first = day - timedelta(days=30)
        flags = self.store.flags(key, metric, first.isoformat(), day.isoformat())
        have = [d for d, _ in self.store.daily(key, metric, first.isoformat(), day.isoformat())]
        n = 0
        for ds in reversed(have):
            if flags.get(ds):
                break
            n += 1
        return n

    def _relearn(self, key, metric, start_day: date, reason: str):
        """Treat everything from start_day on as the new normal for this metric."""
        self.store.set_baseline_start(key, metric, start_day.isoformat(), reason)
        self.store.x("DELETE FROM day_flags WHERE dev=? AND metric=? AND day>=?", (key, metric, start_day.isoformat()))

    # ------------------------------------------------------------------ live data
    def on_sample(self, dev: str, ts: float, w: float, reported: float | None = None, store: bool = True,
                  alive: bool = True):
        """New value. `alive=False` for poll heartbeats: they repeat the value but prove nothing by themselves;
        liveness then comes from HA's `last_reported`."""
        if store:
            self.store.add_sample(dev, ts, w)
        if dev.startswith(HOUSE):
            lv = self.house_live
            lv.ts = max(lv.ts or 0, ts)
            lv.alive = max(lv.alive or 0, ts if alive else 0, reported or 0) or None
            if lv.alive is None or self.clock.now() - lv.alive >= self.cfg.house_stale_after:
                return
            if lv.state != "ok" or lv.entity_missing:
                lv.state, lv.unavail_since, lv.entity_missing = "ok", None, False
            a = self.store.find_open(HOUSE, "hausdaten", self.test)
            if a:
                self._close(a, "erledigt", "Hausdaten kommen wieder an.")
            return
        lv = self.live.get(dev)
        if lv is None:
            return
        d = self.cfg.devices[dev]
        lv.w, lv.ts = w, max(lv.ts or 0, ts)
        if reported:
            lv.reported = max(lv.reported or 0, reported)
        lv.alive = max(lv.alive or 0, ts if alive else 0, reported or 0) or None
        fresh = lv.alive is not None and self.clock.now() - lv.alive < self.cfg.stale_after
        if lv.state != "ok":
            lv.state, lv.unavail_since = "ok", None
        for metric in ("nicht_verfuegbar", "keine_daten"):
            a = self.store.find_open(dev, metric, self.test) if fresh else None
            if a:
                self._close(a, "erledigt", f"{d.name}: Steckdose meldet wieder Werte.")
        # run tracking (for 'runs unusually long')
        if w >= d.an_w:
            if lv.run_start is None or (lv.last_on is not None and ts - lv.last_on > d.luecke):
                lv.run_start = ts
            lv.last_on = ts
        elif lv.run_start is not None and lv.last_on is not None and ts - lv.last_on > d.luecke:
            lv.run_start = None
            a = self.store.find_open(dev, "laufzeit", self.test)
            if a:
                ran = (lv.last_on - (a["details"].get("run_start") or lv.last_on)) / 60
                self._close(a, "erledigt", f"{d.name} ist jetzt aus (lief {explain.fmt_minutes(ran)}).")
        # zero tracking (for 'suddenly uses nothing')
        if w < 0.5:
            lv.zero_since = lv.zero_since or ts
        else:
            lv.zero_since = None
            a = self.store.find_open(dev, "null", self.test)
            if a:
                self._close(a, "erledigt", f"{d.name}: verbraucht wieder Strom ({explain.fmt_num(w, 1)} W).")

    def on_reported(self, dev: str, reported: float):
        lv = self.house_live if dev.startswith(HOUSE) else self.live.get(dev)
        if lv is not None:
            lv.reported = max(lv.reported or 0, reported)
            lv.alive = max(lv.alive or 0, reported)

    def on_unavailable(self, dev: str, ts: float, state: str):
        lv = self.house_live if dev.startswith(HOUSE) else self.live.get(dev)
        if lv is None:
            return
        if lv.state == "ok":
            lv.unavail_since = ts
        lv.state = state

    def on_switch(self, dev: str, on: bool | None):
        lv = self.live.get(dev)
        if lv is not None:
            lv.switch_on = on
            if on is False:
                lv.zero_since = None
                a = self.store.find_open(dev, "null", self.test)
                if a:
                    self._close(a, "erledigt", None)

    def runtime_limit(self, d: Device) -> float | None:
        fb = self.store.override(d.key, "max_laufzeit")
        if fb:
            return fb
        if d.max_laufzeit:
            return d.max_laufzeit
        if d.auto_laufzeit:
            return self.store.override(d.key, "auto_laufzeit")
        return None

    def zero_limit(self, d: Device) -> float | None:
        if d.null_nach:
            return d.null_nach
        if d.typ == "zyklus":
            return self.store.override(d.key, "auto_null")
        return None

    def live_check(self, now: float | None = None) -> list[int]:
        now = self.clock.now() if now is None else now
        new = []
        for key, d in self.cfg.devices.items():
            if not d.aktiv:
                continue
            lv = self.live[key]
            if lv.entity_missing:
                continue
            # unavailable / unknown in HA
            if lv.state != "ok" and lv.unavail_since and now - lv.unavail_since >= self.cfg.unavailable_after:
                if not self.store.find_open(key, "nicht_verfuegbar", self.test):
                    new.append(self._open(key, "nicht_verfuegbar", "system",
                                          explain.unavailable_message(d.name, now - lv.unavail_since, lv.state)))
                continue
            # no values at all any more
            last = lv.alive or 0
            if last and lv.state == "ok" and now - last >= self.cfg.stale_after:
                if not self.store.find_open(key, "keine_daten", self.test):
                    # rarely used plugs often send no new values for hours: shown in the web UI, pushed only if enabled
                    new.append(self._open(key, "keine_daten", "system", explain.stale_message(d.name, now - last),
                                          notify=self.cfg.stale_notify))
                continue
            if lv.state != "ok" or lv.ts is None:
                continue
            # runs unusually long
            limit = self.runtime_limit(d)
            if (limit and lv.run_start is not None and lv.last_on is not None
                    and now - lv.last_on <= max(d.luecke, self.cfg.poll_interval * 2)
                    and now - lv.run_start >= limit and not self.store.find_open(key, "laufzeit", self.test)):
                new.append(self._open(key, "laufzeit", "laufzeit",
                                      explain.runtime_message(d.name, self.profile(key), now - lv.run_start, limit,
                                                              configured=bool(d.max_laufzeit) and
                                                              not self.store.override(key, "max_laufzeit")),
                                      value=(now - lv.run_start) / 60, baseline=limit / 60,
                                      details={"run_start": lv.run_start}))
            # suddenly no consumption although switched on
            zl = self.zero_limit(d)
            if (zl and lv.zero_since is not None and lv.switch_on is not False and now - lv.zero_since >= zl
                    and not self.store.find_open(key, "null", self.test)):
                new.append(self._open(key, "null", "null",
                                      explain.zero_message(d.name, now - lv.zero_since, bool(d.schalter)),
                                      value=0, details={"seit": lv.zero_since}))
        # whole-house data (Growatt)
        if self.cfg.house.hausverbrauch and self.ha_connected:
            hl = self.house_live
            last = hl.alive or 0
            since = hl.unavail_since if hl.state != "ok" else (last or None)
            missing = hl.entity_missing or hl.state != "ok" or not last or now - last >= self.cfg.house_stale_after
            if missing and not self.store.find_open(HOUSE, "hausdaten", self.test):
                started = self.store.meta("started", now)
                ref = since or started
                if now - ref >= self.cfg.house_stale_after:
                    why = (hl.missing_reason or "die Entität fehlt in Home Assistant"
                           if hl.entity_missing else f"Zustand „{hl.state}“" if hl.state != "ok"
                           else "keine neuen Werte")
                    new.append(self._open(HOUSE, "hausdaten", "system",
                                          f"Hausdaten (Hausverbrauch, Netzbezug, Einspeisung) fehlen seit "
                                          f"{explain.fmt_minutes((now - ref) / 60)}: {why}. Grundlast, nicht "
                                          f"zugeordneter Verbrauch und Solaranteil können so nicht ausgewertet "
                                          f"werden – Growatt-Integration prüfen."))
        return new

    def ha_state(self, connected: bool):
        now = self.clock.now()
        if connected and not self.ha_connected:
            if self.ha_down_since and now - self.ha_down_since >= self.cfg.ha_down_after:
                a = self.store.find_open("_system", "ha", self.test)
                if a:
                    self.store.update_anomaly(a["id"], state="erledigt", closed=now)
                frm = self.clock.local(self.ha_down_since).strftime("%d.%m. %H:%M")
                self.notify("Stromfresser-Detektiv: Home Assistant",
                            f"Home Assistant war {explain.fmt_minutes((now - self.ha_down_since) / 60)} nicht "
                            f"erreichbar (ab {frm}). In dieser Zeit fehlen Messwerte.", None, "sfd-ha")
            self.ha_down_since = None
        elif not connected and self.ha_connected:
            self.ha_down_since = now
        elif not connected and self.ha_down_since is None:
            self.ha_down_since = now
        self.ha_connected = connected

    def ha_down_check(self, now=None):
        now = self.clock.now() if now is None else now
        if (not self.ha_connected and self.ha_down_since and now - self.ha_down_since >= self.cfg.ha_down_after
                and not self.store.find_open("_system", "ha", self.test)):
            # cannot be delivered while HA is down; it is queued and sent once HA is back
            self.store.open_anomaly(dev="_system", metric="ha", kind="system", direction=0,
                                    start_day=self.clock.day_of(now).isoformat(), opened=now,
                                    message="Home Assistant nicht erreichbar", test=self.test)

    # ------------------------------------------------------------------ feedback
    def resolve_target(self, target) -> list[dict]:
        t = str(target).strip()
        if t.upper().startswith("SFD_OK_"):
            t = t[7:]
        ids = [int(x) for x in t.replace(",", "_").split("_") if x.isdigit()]
        if ids:
            return [a for a in (self.store.anomaly(i) for i in ids) if a]
        key = None
        for k, d in self.cfg.devices.items():
            if t.lower() in (k.lower(), d.name.lower()):
                key = k
        if t.lower() in ("haus", HOUSE):
            key = HOUSE
        if key is None:
            raise KeyError(f"Unbekanntes Gerät oder unbekannte Meldung: {target}")
        return self.store.anomalies(OPEN_STATES, dev=key, test=self.test)

    def feedback(self, target, source: str = "cli", note: str | None = None) -> list[dict]:
        """'War ok': accept the findings and learn the new behaviour."""
        now = self.clock.now()
        done = []
        for a in self.resolve_target(target):
            if a["state"] not in OPEN_STATES:
                continue
            kind, dev, metric = a["kind"], a["dev"], a["metric"]
            if kind == "taeglich":
                self._relearn(dev, metric, date.fromisoformat(a["start_day"]), "feedback")
                state = "ok"
            elif kind == "programm":
                pid = a["details"].get("programm") or metric.split(":")[1]
                progs = self.store.programs(dev)
                if pid in progs:
                    ds = self.clock.day_bounds(date.fromisoformat(a["start_day"]))[0]
                    accepted = [[round((e - s) / 60, 1), round(wh / 1000, 3)]
                                for s, e, wh, _p, prog, flag in self.store.runs(dev, ds, now + 1) if prog == pid]
                    if accepted:
                        self.store.put_program(dev, pid, progs[pid]["name"], accepted[-30:])
                state = "ok"
            elif kind == "laufzeit":
                lv = self.live.get(dev)
                running = (now - lv.run_start) if (lv and lv.run_start) else (a["value"] or 0) * 60
                new_limit = max(running, (a["value"] or 0) * 60) * 1.5
                self.store.set_override(dev, "max_laufzeit", new_limit)
                state = "ok"
            else:
                state = "stumm"  # muted until the problem is gone; then it can alarm again
            self.store.update_anomaly(a["id"], state=state, closed=now if state == "ok" else None)
            self.store.add_feedback(now, dev, a["id"], source, note)
            done.append(a)
        if done:
            names = sorted({self.name(a["dev"]) for a in done})
            self.notify("Stromfresser-Detektiv: Verstanden",
                        f"„War ok“ für {', '.join(names)} übernommen – das gilt jetzt als normal "
                        f"und ich lerne es dazu.", None, f"sfd-{done[0]['dev']}")
        return done

    # ------------------------------------------------------------------ status
    def learning_progress(self, key: str) -> tuple[int, int]:
        days = [d for d in self.store.daily_days(key)]
        good = 0
        for d in days[-self.cfg.window_days:]:
            if self.store.daily_day(key, d).get("coverage", 0) >= (0.5 if key == HOUSE else MIN_COVERAGE):
                good += 1
        return min(good, self.cfg.min_days), self.cfg.min_days
