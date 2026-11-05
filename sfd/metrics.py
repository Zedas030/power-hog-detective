"""Prometheus text exposition (no client library needed)."""
from __future__ import annotations

from datetime import timedelta

from .detector import HOUSE, OPEN_STATES, Detector
from .features import EVAL_METRICS


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(det: Detector, extra: dict | None = None) -> str:
    cfg, store, clock = det.cfg, det.store, det.clock
    now = clock.now()
    lines = []

    def g(name, help_, rows):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for labels, v in rows:
            if v is None:
                continue
            lab = ",".join(f'{k}="{_esc(x)}"' for k, x in labels.items())
            lines.append(f"{name}{{{lab}}} {float(v):.6g}" if lab else f"{name} {float(v):.6g}")

    devs = [(k, d) for k, d in cfg.devices.items() if d.aktiv]
    g("sfd_device_power_watts", "Aktuelle Leistung je Gerät",
      [({"device": k, "name": d.name}, det.live[k].w) for k, d in devs
       if det.live[k].state == "ok" and not det.live[k].entity_missing])
    g("sfd_device_last_value_age_seconds", "Sekunden seit dem letzten frischen Messwert",
      [({"device": k, "name": d.name}, (now - det.live[k].alive) if det.live[k].alive else None) for k, d in devs])
    g("sfd_device_available", "1 wenn die Entität Werte liefert",
      [({"device": k, "name": d.name}, 1 if det.live[k].state == "ok" and not det.live[k].entity_missing else 0)
       for k, d in devs])
    g("sfd_device_running_seconds", "Laufzeit des aktuellen Laufs",
      [({"device": k, "name": d.name}, (now - det.live[k].run_start) if det.live[k].run_start else 0)
       for k, d in devs])

    yesterday = (clock.local().date() - timedelta(days=1)).isoformat()
    feats, base = [], []
    for key, typ in det.targets():
        name = det.name(key)
        day = store.daily_day(key, yesterday)
        for metric in dict.fromkeys(list(EVAL_METRICS[typ]) + ["kwh"]):  # unique, keeps order
            if metric in day:
                feats.append(({"device": key, "name": name, "metric": metric}, day[metric]))
            if metric in EVAL_METRICS[typ]:
                res = det._eval_metric(key, metric, EVAL_METRICS[typ][metric], clock.local().date(), 0.0)
                if res.baseline is not None:
                    base.append(({"device": key, "name": name, "metric": metric}, res.baseline))
    g("sfd_feature_value", "Tagesmerkmal von gestern", feats)
    g("sfd_feature_baseline", "Gelerntes Normal (Median im gleitenden Fenster)", base)
    g("sfd_learning_days", "Tage mit verwertbaren Daten (bis zur Mindest-Lernzeit)",
      [({"device": k, "name": det.name(k)}, det.learning_progress(k)[0]) for k, _ in det.targets()])

    open_by = {}
    for a in store.anomalies(OPEN_STATES, test=0):
        open_by[a["dev"]] = open_by.get(a["dev"], 0) + 1
    g("sfd_anomalies_open", "Offene Auffälligkeiten je Gerät",
      [({"device": k, "name": det.name(k)}, open_by.get(k, 0)) for k, _ in det.targets()]
      + [({"device": "_system", "name": "System"}, open_by.get("_system", 0))])

    hd = store.daily_day(HOUSE, yesterday)
    g("sfd_house_night_base_watts", "Nächtliche Grundlast Haus (gestern)", [({}, hd.get("nacht_w"))])
    g("sfd_house_unassigned_night_watts", "Nächtliche Grundlast ohne Messsteckdose (gestern)",
      [({}, hd.get("unzugeordnet_nacht_w"))])
    g("sfd_house_data_age_seconds", "Sekunden seit dem letzten Hausdaten-Wert",
      [({}, (now - det.house_live.alive) if det.house_live.alive else None)])
    g("sfd_ha_connected", "1 wenn die WebSocket-Verbindung zu Home Assistant steht", [({}, 1 if det.ha_connected else 0)])
    g("sfd_notifications_total", "Erzeugte Meldungen seit Start", [({}, det.notifications)])
    g("sfd_outbox_pending", "Noch nicht zugestellte Meldungen", [({}, len(store.pending()))])
    g("sfd_db_size_bytes", "Größe der SQLite-Datenbank", [({}, store.size_bytes())])
    for k, v in (extra or {}).items():
        g(f"sfd_{k}", k, [({}, v)])
    return "\n".join(lines) + "\n"
