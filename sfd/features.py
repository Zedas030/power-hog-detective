"""Daily features per device type, computed from the power signal of one local day."""
from __future__ import annotations

from .config import Device
from .segment import Run, covered, energy_wh, runs, steps, time_weighted
from .util import median, quantile, weighted_median

MIN_COVERAGE = 0.8  # days with less data are not used for learning or alarms

# metric -> (minimum absolute deviation, relative threshold or None to use the global one,
#            relative check enabled, circular minutes)
EVAL_METRICS = {
    "zyklus": {
        "zyklus_min": (2.0, None, True, False),
        "pause_min": (3.0, None, True, False),
        "zyklen": (3.0, None, True, False),
        "laufanteil": (0.05, None, True, False),
        "kwh": (0.1, None, True, False),
    },
    "dauerlast": {"grundlast_w": (3.0, 0.15, True, False)},
    "programm": {"standby_w": (2.0, None, True, False)},
    "nutzung": {"standby_w": (2.0, None, True, False)},
    "ladegeraet": {"standby_w": (1.5, None, True, False)},
    "zeitplan": {
        "ein_min": (30.0, None, False, True),
        "betrieb_w": (5.0, 0.2, True, False),
    },
    "haus": {
        "nacht_w": (20.0, 0.15, True, False),
        "unzugeordnet_nacht_w": (20.0, 0.25, True, False),
    },
}

# when several metrics of one device deviate, the first in this order leads the message
PRIORITY = ["zyklus_min", "pause_min", "zyklen", "laufanteil", "grundlast_w", "standby_w", "ein_min",
            "betrieb_w", "unzugeordnet_nacht_w", "nacht_w", "kwh"]


def device_day(dev: Device, samples, day_start: float, day_end: float, data_end: float,
               max_gap: float = 600.0):
    """Return (metrics, runs_starting_in_day). `samples` should extend ~12 h around the day."""
    if not samples:
        return {"coverage": 0.0}, []
    ext_start = min(day_start, samples[0][0])
    ext_end = min(max(day_end, samples[-1][0] + max_gap), data_end)
    st_ext = steps(samples, ext_start, ext_end, max_gap)
    st_day = [(max(a, day_start), min(b, day_end), w) for a, b, w in st_ext if b > day_start and a < day_end]
    cov = covered(st_day) / (day_end - day_start)
    m: dict = {"coverage": round(cov, 4), "kwh": round(energy_wh(st_day) / 1000, 4)}
    all_runs = runs(st_ext, dev.an_w, dev.luecke, dev.min_lauf, data_end=data_end)
    day_runs = [r for r in all_runs if day_start <= r.start < day_end]
    on_s = sum(min(r.end, day_end) - max(r.start, day_start) for r in all_runs
               if r.end > day_start and r.start < day_end)
    m["an_stunden"] = round(on_s / 3600, 3)

    off = time_weighted(st_day, lambda w: w < dev.an_w)
    if sum(s for _, s in off) >= 3600:
        m["standby_w"] = round(weighted_median(off), 2)

    closed = [r for r in day_runs if not r.open]
    t = dev.typ
    if t == "zyklus":
        _cycle_features(m, all_runs, closed, day_start, day_end, st_day)
    elif t == "dauerlast":
        tw = time_weighted(st_day)
        if tw:
            m["grundlast_w"] = round(weighted_median(tw), 2)
            m["p95_w"] = round(quantile([w for w, _ in tw], 0.95), 2)
    elif t == "programm":
        m["laeufe"] = len(day_runs)
        if closed:
            m["kwh_pro_lauf"] = round(median([r.wh for r in closed]) / 1000, 3)
            m["lauf_min"] = round(median([r.minutes for r in closed]), 1)
    elif t == "ladegeraet":
        m["ladungen"] = len(day_runs)
    elif t == "zeitplan":
        if day_runs:
            main = max(day_runs, key=lambda r: r.end - r.start)
            m["ein_min"] = round((main.start - day_start) / 60, 1)
            m["aus_min"] = round((main.end - day_start) / 60, 1)
            on_w = [(w, b - a) for a, b, w in st_ext if a >= main.start and b <= main.end and w >= dev.an_w]
            if on_w:
                m["betrieb_w"] = round(weighted_median(on_w), 1)
    if t in ("nutzung", "programm", "ladegeraet", "zeitplan"):
        m["laeufe"] = len(day_runs)
    return m, day_runs


def _cycle_features(m, all_runs, closed, day_start, day_end, st_day):
    m["zyklen"] = len([r for r in all_runs if day_start <= r.start < day_end])
    if len(closed) >= 3:
        m["zyklus_min"] = round(median([r.minutes for r in closed]), 2)
    pauses = []
    ordered = sorted(all_runs, key=lambda r: r.start)
    for a, b in zip(ordered, ordered[1:]):
        if day_start <= b.start < day_end and not a.open:
            pauses.append((b.start - a.end) / 60)
    if len(pauses) >= 3:
        m["pause_min"] = round(median(pauses), 2)
    cov = covered(st_day)
    if cov > 0:
        on = sum(min(r.end, day_end) - max(r.start, day_start) for r in all_runs
                 if r.end > day_start and r.start < day_end)
        m["laufanteil"] = round(on / cov, 4)
    if closed:
        m["spitze_w"] = round(median([r.peak for r in closed]), 1)


def house_day(house_samples, grid_samples, export_samples, plug_samples: dict, day_start: float,
              day_end: float, night_from_min: int, night_to_min: int, dle_threshold_w: float | None,
              max_gap: float = 900.0):
    """Whole-house features. house/grid/export are sample lists; plug_samples: dev -> samples."""
    m: dict = {}
    if not house_samples:
        return m
    st = steps(house_samples, day_start, day_end, max_gap)
    cov = covered(st) / (day_end - day_start)
    m["coverage"] = round(cov, 4)
    if cov < 0.5:
        return m
    m["kwh"] = round(energy_wh(st) / 1000, 3)
    if grid_samples:
        m["netz_kwh"] = round(energy_wh(steps(grid_samples, day_start, day_end, max_gap)) / 1000, 3)
    if export_samples:
        m["einspeisung_kwh"] = round(energy_wh(steps(export_samples, day_start, day_end, max_gap)) / 1000, 3)

    n0, n1 = day_start + night_from_min * 60, day_start + night_to_min * 60
    night = [(max(a, n0), min(b, n1), w) for a, b, w in st if b > n0 and a < n1]
    if covered(night) >= 0.6 * (n1 - n0):
        # night base = median of the house load in the night window
        m["nacht_w"] = round(weighted_median(time_weighted(night)), 1)
        plug_steps = []
        missing = 0
        for dev, s in plug_samples.items():
            pst = steps(s, n0, n1, 600)
            if covered(pst) <= 0:
                missing += 1  # plug without data (offline) counts as 0 W
                continue
            plug_steps.append(pst)
        # compare house and plugs at the same moments: cycling devices (fridge) would otherwise distort it
        diffs, plug_vals = [], []
        for a, b, w in night:
            t = (a + b) / 2
            p = sum(_value_at(ps, t) for ps in plug_steps)
            diffs.append((w - p, b - a))
            plug_vals.append((p, b - a))
        m["steckdosen_nacht_w"] = round(weighted_median(plug_vals), 1)
        m["steckdosen_ohne_daten"] = missing
        m["unzugeordnet_nacht_w"] = round(weighted_median(diffs), 1)

    if dle_threshold_w:
        # Unassigned high load (house minus plugs) = likely instantaneous water heater
        plug_steps = {d: steps(s, day_start, day_end, 600) for d, s in plug_samples.items()}
        base = m.get("unzugeordnet_nacht_w", 0.0) or 0.0
        dle_wh, events, in_event = 0.0, 0, False
        for a, b, w in st:
            plugs = sum(_value_at(ps, (a + b) / 2) for ps in plug_steps.values())
            un = w - plugs - base
            if un >= dle_threshold_w:
                dle_wh += (b - a) * un / 3600
                if not in_event:
                    events += 1
                in_event = True
            else:
                in_event = False
        m["dle_kwh"] = round(dle_wh / 1000, 3)
        m["dle_vorgaenge"] = events
    return m


def _value_at(st, t):
    # st sorted; linear scan is fine for a few thousand intervals per day
    lo, hi = 0, len(st) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a, b, w = st[mid]
        if t < a:
            hi = mid - 1
        elif t >= b:
            lo = mid + 1
        else:
            return w
    return 0.0


def learned_runtime_limit(run_minutes: list[float]) -> float | None:
    """Learned 'unusually long' limit in seconds from historic run lengths (minutes)."""
    if len(run_minutes) < 10:
        return None
    p99 = quantile(run_minutes, 0.99)
    mx = max(run_minutes)
    return max(p99 * 1.5, mx * 1.2, 30.0) * 60


def learned_zero_limit(pause_minutes: list[float]) -> float | None:
    """For cycle devices: no cycle for 3x the longest usual pause (at least 4 h) means 'dead'."""
    if len(pause_minutes) < 10:
        return None
    return max(quantile(pause_minutes, 0.95) * 3, 240.0) * 60


Run = Run  # re-export for type hints elsewhere
