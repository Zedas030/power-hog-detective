"""Generates the Grafana dashboard.

    python grafana/make_dashboard.py            -> grafana/power-hog-detective.json (English)
    python grafana/make_dashboard.py --lang de  -> German titles
"""
import json
import sys

DE = "--lang" in sys.argv and sys.argv[sys.argv.index("--lang") + 1] == "de"


def T(en, de):
    return de if DE else en

from pathlib import Path

DS = {"type": "prometheus", "uid": "${datasource}"}
panels = []
pid = 1


def panel(kind, title, exprs, x, y, w, h, unit=None, legend="{{name}}", extra=None, desc=None):
    global pid
    p = {"id": pid, "type": kind, "title": title, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"datasource": DS, "expr": e, "legendFormat": lf if lf is not None else legend,
                      "refId": chr(65 + i), **({"instant": True} if kind in ("stat", "bargauge", "table") else {})}
                     for i, (e, lf) in enumerate(exprs)],
         "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []},
         "options": {}}
    if desc:
        p["description"] = desc
    if extra:
        for k, v in extra.items():
            if isinstance(v, dict) and isinstance(p.get(k), dict):
                p[k].update(v)
            else:
                p[k] = v
    panels.append(p)
    pid += 1


panel("stat", T("Open findings", "Offene Auffälligkeiten"), [("sum(sfd_anomalies_open)", T("open", "offen"))], 0, 0, 4, 4,
      extra={"fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [
          {"color": "green", "value": None}, {"color": "red", "value": 1}]}}, "overrides": []}})
panel("stat", T("Home Assistant connected", "Home Assistant verbunden"), [("sfd_ha_connected", "HA")], 4, 0, 4, 4,
      extra={"fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {
          "0": {"text": T("disconnected", "getrennt"), "color": "red"}, "1": {"text": T("connected", "verbunden"), "color": "green"}}}]},
          "overrides": []}})
panel("stat", T("Night base load (yesterday)", "Nächtliche Grundlast (gestern)"), [("sfd_house_night_base_watts", T("house", "Haus"))], 8, 0, 4, 4, unit="watt")
panel("stat", T("of which without metering plug", "davon ohne Messsteckdose"), [("sfd_house_unassigned_night_watts", T("unassigned", "nicht zugeordnet"))], 12, 0, 4, 4,
      unit="watt")
panel("stat", T("Notifications queued", "Meldungen in Warteschlange"), [("sfd_outbox_pending", T("pending", "wartend"))], 16, 0, 4, 4)
panel("stat", T("Database", "Datenbank"), [("sfd_db_size_bytes", "SQLite")], 20, 0, 4, 4, unit="bytes")
panel("timeseries", T("Power per device", "Leistung je Gerät"), [("sfd_device_power_watts", None)], 0, 4, 24, 9, unit="watt",
      extra={"fieldConfig": {"defaults": {"custom": {"fillOpacity": 8, "lineWidth": 1}}, "overrides": []}})
panel("timeseries", T("Feature and learned normal: $device / $metric", "Merkmal und gelerntes Normal: $device / $metric"),
      [('sfd_feature_value{device="$device",metric="$metric"}', T("yesterday", "gestern")),
       ('sfd_feature_baseline{device="$device",metric="$metric"}', T("learned normal", "gelerntes Normal"))], 0, 13, 12, 8,
      desc=T("Daily feature (e.g. cycle length in minutes) against the median of the sliding learning window",
             "Tagesmerkmal (z. B. Zyklusdauer in Minuten) gegen den Median im gleitenden Lernfenster"))
panel("timeseries", T("Night base load", "Grundlast nachts"), [("sfd_house_night_base_watts", T("whole house", "Haus gesamt")),
                                         ("sfd_house_unassigned_night_watts", T("not attributable to any plug", "keiner Steckdose zuzuordnen"))],
      12, 13, 12, 8, unit="watt")
panel("bargauge", T("Learning progress (days)", "Lernfortschritt (Tage)"), [("sfd_learning_days", None)], 0, 21, 8, 9,
      extra={"options": {"orientation": "horizontal", "displayMode": "gradient"},
             "fieldConfig": {"defaults": {"min": 0, "max": 14}, "overrides": []}})
panel("bargauge", T("Age of the last reading", "Alter des letzten Messwerts"), [("sfd_device_last_value_age_seconds", None)], 8, 21, 8, 9, unit="s",
      extra={"options": {"orientation": "horizontal", "displayMode": "basic"},
             "fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [
                 {"color": "green", "value": None}, {"color": "orange", "value": 600},
                 {"color": "red", "value": 1800}]}}, "overrides": []}})
panel("bargauge", T("Open findings per device", "Offene Auffälligkeiten je Gerät"), [("sfd_anomalies_open", None)], 16, 21, 8, 9,
      extra={"options": {"orientation": "horizontal", "displayMode": "basic"}})

dash = {
    "uid": T("power-hog-detective", "stromfresser-detektiv"),
    "title": T("Power Hog Detective", "Stromfresser-Detektiv"),
    "tags": T(["energy", "power-hog"], ["energie", "stromfresser"]),
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "1m",
    "time": {"from": "now-2d", "to": "now"},
    "templating": {"list": [
        {"name": "datasource", "type": "datasource", "query": "prometheus", "label": T("Data source", "Datenquelle"),
         "current": {}, "hide": 0},
        {"name": "device", "type": "query", "label": T("Device", "Gerät"), "datasource": DS,
         "query": {"query": "label_values(sfd_feature_value, device)", "refId": "A"},
         "definition": "label_values(sfd_feature_value, device)", "refresh": 2, "current": {}},
        {"name": "metric", "type": "query", "label": T("Feature", "Merkmal"), "datasource": DS,
         "query": {"query": 'label_values(sfd_feature_value{device="$device"}, metric)', "refId": "B"},
         "definition": 'label_values(sfd_feature_value{device="$device"}, metric)', "refresh": 2, "current": {}},
    ]},
    "panels": panels,
}
Path(__file__).with_name(T("power-hog-detective.json", "stromfresser-detektiv.de.json")).write_text(json.dumps(dash, ensure_ascii=False, indent=1),
                                                                  encoding="utf-8")
print("ok", len(panels), "panels")
