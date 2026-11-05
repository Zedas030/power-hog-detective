"""Acceptance scenarios: synthetic faults on top of the REAL learned baseline, run on a copy of the database."""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path

from . import synth
from .detector import HOUSE, Detector
from .explain import profile_for
from .features import _value_at
from .segment import steps
from .store import Store
from .util import Clock, fmt_num, median


def _copy_store(src: Store) -> tuple[Store, str]:
    src.flush()
    # next to the real database (the container's /tmp is a small RAM disk); removed again in Lab.close()
    base = Path(src.path).parent if src.path != ":memory:" else Path(tempfile.gettempdir())
    tmp = Path(tempfile.mkdtemp(prefix="szenario-", dir=base)) / "kopie.sqlite"
    dst = sqlite3.connect(str(tmp))
    src.db.backup(dst)
    dst.close()
    return Store(str(tmp)), str(tmp)


def _learned(store: Store, dev: str, metric: str, last_day, days=28):
    first = (last_day - timedelta(days=days)).isoformat()
    vals = [v for _, v in store.daily(dev, metric, first, last_day.isoformat())]
    return median(vals), len(vals)


class Lab:
    """A sandbox: database copy + simulated clock + a detector in test mode."""

    def __init__(self, cfg, real_store: Store, outbox_notifier=None):
        self.cfg = cfg
        self.store, self.path = _copy_store(real_store)
        self.clock = Clock(cfg.tz)
        self.messages = []

        def notify(title, message, action, tag):
            self.messages.append({"title": title, "message": message, "action": action})
            if outbox_notifier:
                outbox_notifier(title, message, action, tag)

        self.det = Detector(cfg, self.store, self.clock, notify=notify, test=True)
        self.today = Clock(cfg.tz).local().date()
        self.last_real = self.today - timedelta(days=1)

    def wipe_from(self, dev: str, day):
        s = self.clock.day_bounds(day)[0]
        self.store.x("DELETE FROM samples WHERE dev=? AND ts>=?", (dev, s))

    def close(self):
        import shutil
        self.store.close()
        shutil.rmtree(Path(self.path).parent, ignore_errors=True)


def scenario_compressor(cfg, real_store, notifier=None, device: str | None = None, factor: float = 1.4,
                        n_days: int = 5) -> dict:
    lab = Lab(cfg, real_store, notifier)
    det, st, clock = lab.det, lab.store, lab.clock
    key = device or next(k for k, d in cfg.devices.items()
                         if d.typ == "zyklus" and profile_for(d.name, d.profil) == "kuehlgeraet")
    d = cfg.devices[key]
    cyc, n = _learned(st, key, "zyklus_min", lab.last_real)
    pause, _ = _learned(st, key, "pause_min", lab.last_real)
    peak, _ = _learned(st, key, "spitze_w", lab.last_real)
    standby, _ = _learned(st, key, "standby_w", lab.last_real)
    if not cyc or not pause:
        raise SystemExit(f"{d.name}: noch keine gelernten Zyklen – erst vorlernen.")
    lab.wipe_from(key, lab.today)
    s = clock.day_bounds(lab.today)[0]
    e = clock.day_bounds(lab.today + timedelta(days=n_days - 1))[1]
    st.add_samples(key, synth.fridge(s, e, cycle_min=cyc * factor, pause_min=pause, on_w=(peak or 80) / 1.05,
                                     standby_w=standby or 1.0, seed=42))
    days_out = []
    for i in range(n_days):
        day = lab.today + timedelta(days=i)
        clock.set(clock.at(day + timedelta(days=1), 0, 20))
        det.process_day(day, house=False, aggregates=False)
        before = len(lab.messages)
        det.evaluate_day(day, only={key})
        f = st.daily_day(key, day.isoformat())
        days_out.append({"tag": day.isoformat(), "zyklus_min": f.get("zyklus_min"), "pause_min": f.get("pause_min"),
                         "meldung": [m["message"] for m in lab.messages[before:]]})
    res = {"szenario": "Längere Kompressorzyklen", "geraet": d.name, "gelernt_aus_tagen": n,
           "gelernt": {"zyklus_min": cyc, "pause_min": pause, "spitze_w": peak},
           "eingespielt": f"Zyklen {fmt_num(factor * 100 - 100)} % länger ({fmt_num(cyc * factor, 1)} statt "
                          f"{fmt_num(cyc, 1)} Min.), Pausen unverändert, {n_days} Tage",
           "tage": days_out, "meldungen": lab.messages}
    # feedback in the sandbox: 'war ok' -> the new cycle length is learned, no further alarm
    if lab.messages:
        n0 = len(lab.messages)
        det.feedback(next(m["action"] for m in lab.messages if m["action"]).replace("SFD_TEST_", ""),
                     source="szenario")
        e2 = clock.day_bounds(lab.today + timedelta(days=n_days + 4))[1]
        st.add_samples(key, synth.fridge(e, e2, cycle_min=cyc * factor, pause_min=pause,
                                         on_w=(peak or 80) / 1.05, standby_w=standby or 1.0, seed=43))
        for i in range(n_days, n_days + 5):
            day = lab.today + timedelta(days=i)
            clock.set(clock.at(day + timedelta(days=1), 0, 20))
            det.process_day(day, house=False, aggregates=False)
            det.evaluate_day(day, only={key})
        res["nach_feedback"] = {"bestaetigung": lab.messages[n0]["message"] if len(lab.messages) > n0 else None,
                                "weitere_meldungen_5_tage": [m["message"] for m in lab.messages[n0 + 1:]]}
    lab.close()
    return res


def scenario_baseload(cfg, real_store, notifier=None, extra_w: float = 35.0, n_days: int = 4,
                      unmetered_w: float = 100.0, learn_days: int = 16) -> dict:
    """Night base load rises by `extra_w` without any plug seeing it.

    There is no whole-house meter in HA yet, so the meter is SIMULATED: house = the real plug curves of the
    last days + a constant unmetered load (`unmetered_w`, e.g. fridge-less kitchen, lights, network) and, for the
    last `n_days`, + `extra_w`. The detector has to learn the normal from the learning days and then explain the jump.
    """
    import copy
    import random
    cfg = copy.deepcopy(cfg)
    cfg.house.hausverbrauch = cfg.house.hausverbrauch or "sensor.simulierter_hauszaehler"
    lab = Lab(cfg, real_store, notifier)
    det, st, clock = lab.det, lab.store, lab.clock
    for t in ("samples", "quarter", "energy_hourly"):
        st.x(f"DELETE FROM {t} WHERE dev LIKE '_haus%'")
    for t in ("daily", "day_flags", "anomalies", "baseline_start"):
        st.x(f"DELETE FROM {t} WHERE dev = '_haus'")
    first = lab.today - timedelta(days=learn_days + n_days)
    rnd = random.Random(7)
    out_days = []
    for i in range(learn_days + n_days):
        day = first + timedelta(days=i)
        s, e = clock.day_bounds(day)
        series = {}
        for k in cfg.devices:
            rows = st.samples(k, s, e, with_previous=False)
            if len(rows) < 100:  # raw values already condensed: use the minute averages
                rows = [(t, w) for t, w, _mx in st.aggregates("minute", k, s, e)]
                st.add_samples(k, rows)
            series[k] = rows
        extra = extra_w if i >= learn_days else 0.0
        # plug values exactly as the detector sees them (a value is held at most 10 min, offline = 0 W)
        plug_steps = [steps(st.samples(k, s, e), s, e, 600) for k in series]
        house, t = [], s
        while t < e:
            plugs = sum(_value_at(ps, t) for ps in plug_steps)
            house.append((t, plugs + unmetered_w + extra + rnd.uniform(-4, 4)))
            t += 60
        st.add_samples(f"{HOUSE}:hausverbrauch", house)
        clock.set(clock.at(day + timedelta(days=1), 0, 20))
        det.silent = i < learn_days
        det.process_day(day, aggregates=False, devices=False)
        before = len(lab.messages)
        det.evaluate_day(day, only={HOUSE})
        f = st.daily_day(HOUSE, day.isoformat())
        if i >= learn_days - 2:
            out_days.append({"tag": day.isoformat(), "phase": "Störung" if extra else "normal",
                             "nacht_w": f.get("nacht_w"), "steckdosen_nacht_w": f.get("steckdosen_nacht_w"),
                             "unzugeordnet_nacht_w": f.get("unzugeordnet_nacht_w"),
                             "meldung": [m["message"] for m in lab.messages[before:]]})
    det.silent = False
    learned = st.daily(HOUSE, "unzugeordnet_nacht_w", first.isoformat(),
                       (first + timedelta(days=learn_days - 1)).isoformat())
    res = {"szenario": "Gestiegene nächtliche Grundlast",
           "hinweis": "Hauszähler simuliert (in HA gibt es keinen echten), Steckdosen = echte Verläufe",
           "gelernt_aus_tagen": len(learned),
           "gelernt": {"unzugeordnet_nacht_w": median([v for _, v in learned])},
           "eingespielt": f"+{fmt_num(extra_w)} W Dauerlast ohne Messsteckdose ab "
                          f"{(first + timedelta(days=learn_days)).isoformat()}",
           "tage": out_days, "meldungen": lab.messages}
    lab.close()
    return res


def scenario_runtime(cfg, real_store, notifier=None, device: str | None = None, watts: float | None = None) -> dict:
    lab = Lab(cfg, real_store, notifier)
    det, st, clock = lab.det, lab.store, lab.clock
    key = device or next((k for k, d in cfg.devices.items() if d.max_laufzeit), None) or \
        next(k for k, d in cfg.devices.items() if d.auto_laufzeit)
    d = cfg.devices[key]
    limit = det.runtime_limit(d)
    if not limit:
        raise SystemExit(f"{d.name}: keine Laufzeitgrenze (max_laufzeit setzen oder erst vorlernen).")
    w = watts or max(d.an_w * 4, 60.0)
    t0 = clock.at(lab.today, 0, 5) + 86400  # tomorrow 00:05 in simulated time
    det.live[key] = type(det.live[key])()
    det.on_switch(key, True)
    timeline = []
    t = t0
    end = t0 + limit + 20 * 60
    while t <= end:
        clock.set(t)
        det.on_sample(key, t, w, alive=True)
        before = len(lab.messages)
        det.live_check()
        for m in lab.messages[before:]:
            timeline.append({"nach_min": round((t - t0) / 60), "meldung": m["message"]})
        t += 60
    # switch off -> "ist jetzt aus"
    for k in range(1, 3 + int(d.luecke // 60) + 1):
        tt = end + k * 60
        clock.set(tt)
        before = len(lab.messages)
        det.on_sample(key, tt, 0.3, alive=True)
        det.live_check()
        for m in lab.messages[before:]:
            timeline.append({"nach_min": round((tt - t0) / 60), "meldung": m["message"]})
    res = {"szenario": "Gerät läuft zu lange", "geraet": d.name,
           "grenze_min": round(limit / 60), "grenze_quelle": "config max_laufzeit" if d.max_laufzeit else "gelernt",
           "eingespielt": f"{fmt_num(w)} W ununterbrochen ab 00:05 für {fmt_num((end - t0) / 3600, 1)} Std., "
                          f"danach aus", "verlauf": timeline, "meldungen": lab.messages}
    lab.close()
    return res


SCENARIOS = {"kompressor": scenario_compressor, "grundlast": scenario_baseload, "laufzeit": scenario_runtime}
