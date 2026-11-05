"""Pre-learning from existing history: Prometheus (fine-grained), HA recorder and HA long-term statistics."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import aiohttp

from .config import Config
from .detector import HOUSE, HOUSE_ROLES, Detector
from .store import Store
from .util import Clock

log = logging.getLogger("sfd.history")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class Prometheus:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    async def range(self, session, selector: str, start: float, end: float, step: float):
        out = []
        t = start
        chunk = step * 10000
        while t < end:
            t2 = min(end, t + chunk)
            params = {"query": selector, "start": f"{t:.0f}", "end": f"{t2:.0f}", "step": f"{step:.0f}s"}
            async with session.get(f"{self.url}/api/v1/query_range", params=params) as r:
                js = await r.json()
            if js.get("status") != "success":
                raise RuntimeError(f"Prometheus: {js.get('error')}")
            for series in js["data"]["result"]:
                out += [(float(ts), float(v)) for ts, v in series["values"]]
            t = t2 + step
        out.sort()
        dedup = []
        for ts, v in out:
            if dedup and abs(dedup[-1][0] - ts) < 1:
                continue
            dedup.append((ts, v))
        return dedup


def _selectors(entity: str, extra: list, friendly: str | None):
    sels = []
    base = f'entity="{entity}"' + (f',friendly_name="{friendly}"' if friendly else "")
    sels.append(f"homeassistant_sensor_power_w{{{base}}}")
    for v in extra or []:
        if isinstance(v, str):
            v = {"entity": v}
        s = f'entity="{v["entity"]}"'
        if v.get("friendly_name"):
            s += f',friendly_name="{v["friendly_name"]}"'
        sels.append(f"homeassistant_sensor_power_w{{{s}}}")
    return sels


async def prelearn(cfg: Config, store: Store, det: Detector, clock: Clock, ha=None, days: int | None = None,
                   friendly_names: dict | None = None, progress=print) -> dict:
    """Load history, compute features for each past day and replay the detection silently."""
    days = days or cfg.prelearn_days
    today = clock.local().date()
    first = today - timedelta(days=days)
    start = clock.day_bounds(first)[0]
    end = clock.now()
    friendly_names = friendly_names or {}
    report = {"tage": days, "quellen": {}, "geraete": {}}

    if cfg.prometheus_url:
        prom = Prometheus(cfg.prometheus_url)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
            for key, d in cfg.devices.items():
                if not d.aktiv:
                    continue
                total = []
                for sel in _selectors(d.leistung, d.verlauf, friendly_names.get(d.leistung)):
                    total += await prom.range(s, sel, start, end, 15)
                total.sort()
                store.add_samples(key, total)
                report["geraete"][key] = len(total)
                progress(f"  {d.name}: {len(total)} Messpunkte aus Prometheus")
            for role in HOUSE_ROLES:
                ent = getattr(cfg.house, role)
                if not ent:
                    continue
                total = []
                for sel in _selectors(ent, cfg.house.verlauf.get(role), None):
                    total += await prom.range(s, sel, start, end, 60)
                total.sort()
                store.add_samples(f"{HOUSE}:{role}", total)
                report["geraete"][f"{HOUSE}:{role}"] = len(total)
                progress(f"  Haus/{role}: {len(total)} Messpunkte aus Prometheus")
        report["quellen"]["prometheus"] = True

    if ha is not None:
        # HA recorder: fills in devices that Prometheus did not have
        missing = [d for k, d in cfg.devices.items() if d.aktiv and report["geraete"].get(k, 0) < 100]
        if missing:
            hist = await ha.history([d.leistung for d in missing], _iso(start), _iso(end))
            for d in missing:
                rows = []
                for st in hist.get(d.leistung, []):
                    try:
                        rows.append((float(st.get("lu") or st.get("lc")), float(st["s"])))
                    except (TypeError, ValueError, KeyError):
                        continue
                rows = _resample_hold(rows, end)
                store.add_samples(d.key, rows)
                report["geraete"][d.key] = len(rows)
                progress(f"  {d.name}: {len(rows)} Messpunkte aus dem HA-Recorder")
            report["quellen"]["recorder"] = True
        # long-term statistics: hourly energy for monthly reports (as far back as HA has them)
        n = await import_statistics(cfg, store, clock, ha)
        report["quellen"]["statistik_stunden"] = n
        progress(f"  HA-Langzeitstatistik: {n} Stundenwerte übernommen")

    store.flush()
    progress("  Merkmale je Tag berechnen und Erkennung nachspielen …")
    d = first
    det.silent = True
    try:
        while d < today:
            det.process_day(d, data_end=end)
            det.evaluate_day(d, notify=False)
            d += timedelta(days=1)
    finally:
        det.silent = False
    store.set_meta("vorgelernt", {"am": clock.now(), "von": first.isoformat(), "bis": (today - timedelta(days=1)).isoformat()})
    store.compact(clock.now(), cfg.raw_days, cfg.minute_days, cfg.quarter_days)
    return report


def _resample_hold(rows, end, step=60.0, max_hold=6 * 3600):
    """Recorder only stores changes: repeat each value every `step` s (up to max_hold) like a heartbeat."""
    rows = sorted(rows)
    out = []
    for i, (ts, w) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else end
        t = ts
        lim = min(nxt, ts + max_hold)
        while t < lim:
            out.append((t, w))
            t += step
    return out


async def import_statistics(cfg: Config, store: Store, clock: Clock, ha, since: date | None = None) -> int:
    """Hourly mean power (W) from HA long-term statistics -> energy_hourly (Wh), without overwriting measurements."""
    ids = {d.leistung: d.key for d in cfg.devices.values() if d.aktiv}
    for role in HOUSE_ROLES:
        ent = getattr(cfg.house, role)
        if ent:
            ids[ent] = f"{HOUSE}:{role}"
    for ent in cfg.house.solar:
        ids[ent] = f"_solar:{ent}"
    since = since or (clock.local().date() - timedelta(days=400))
    res = await ha.statistics(list(ids), _iso(clock.day_bounds(since)[0]), _iso(clock.now()), "hour")
    n = 0
    for ent, rows in (res or {}).items():
        dev = ids.get(ent)
        if not dev:
            continue
        vals = []
        for r in rows:
            if r.get("mean") is None:
                continue
            ts = r["start"] / 1000 if isinstance(r["start"], (int, float)) else \
                datetime.fromisoformat(r["start"]).timestamp()
            vals.append((ts, float(r["mean"])))
        if dev.startswith("_solar:"):
            store.put_energy(dev, vals, "statistik")
        else:
            store.put_energy_if_missing(dev, vals, "statistik")
        n += len(vals)
    return n
