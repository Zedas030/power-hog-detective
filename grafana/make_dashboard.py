"""Generates grafana/stromfresser-detektiv.json (run: python grafana/make_dashboard.py)."""
import json
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


panel("stat", "Offene Auffälligkeiten", [("sum(sfd_anomalies_open)", "offen")], 0, 0, 4, 4,
      extra={"fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [
          {"color": "green", "value": None}, {"color": "red", "value": 1}]}}, "overrides": []}})
panel("stat", "Home Assistant verbunden", [("sfd_ha_connected", "HA")], 4, 0, 4, 4,
      extra={"fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {
          "0": {"text": "getrennt", "color": "red"}, "1": {"text": "verbunden", "color": "green"}}}]},
          "overrides": []}})
panel("stat", "Nächtliche Grundlast (gestern)", [("sfd_house_night_base_watts", "Haus")], 8, 0, 4, 4, unit="watt")
panel("stat", "davon ohne Messsteckdose", [("sfd_house_unassigned_night_watts", "nicht zugeordnet")], 12, 0, 4, 4,
      unit="watt")
panel("stat", "Meldungen in Warteschlange", [("sfd_outbox_pending", "wartend")], 16, 0, 4, 4)
panel("stat", "Datenbank", [("sfd_db_size_bytes", "SQLite")], 20, 0, 4, 4, unit="bytes")
panel("timeseries", "Leistung je Gerät", [("sfd_device_power_watts", None)], 0, 4, 24, 9, unit="watt",
      extra={"fieldConfig": {"defaults": {"custom": {"fillOpacity": 8, "lineWidth": 1}}, "overrides": []}})
panel("timeseries", "Merkmal und gelerntes Normal: $device / $metric",
      [('sfd_feature_value{device="$device",metric="$metric"}', "gestern"),
       ('sfd_feature_baseline{device="$device",metric="$metric"}', "gelerntes Normal")], 0, 13, 12, 8,
      desc="Tagesmerkmal (z. B. Zyklusdauer in Minuten) gegen den Median im gleitenden Lernfenster")
panel("timeseries", "Grundlast nachts", [("sfd_house_night_base_watts", "Haus gesamt"),
                                         ("sfd_house_unassigned_night_watts", "keiner Steckdose zuzuordnen")],
      12, 13, 12, 8, unit="watt")
panel("bargauge", "Lernfortschritt (Tage)", [("sfd_learning_days", None)], 0, 21, 8, 9,
      extra={"options": {"orientation": "horizontal", "displayMode": "gradient"},
             "fieldConfig": {"defaults": {"min": 0, "max": 14}, "overrides": []}})
panel("bargauge", "Alter des letzten Messwerts", [("sfd_device_last_value_age_seconds", None)], 8, 21, 8, 9, unit="s",
      extra={"options": {"orientation": "horizontal", "displayMode": "basic"},
             "fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [
                 {"color": "green", "value": None}, {"color": "orange", "value": 600},
                 {"color": "red", "value": 1800}]}}, "overrides": []}})
panel("bargauge", "Offene Auffälligkeiten je Gerät", [("sfd_anomalies_open", None)], 16, 21, 8, 9,
      extra={"options": {"orientation": "horizontal", "displayMode": "basic"}})

dash = {
    "uid": "stromfresser-detektiv",
    "title": "Stromfresser-Detektiv",
    "tags": ["energie", "stromfresser"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "1m",
    "time": {"from": "now-2d", "to": "now"},
    "templating": {"list": [
        {"name": "datasource", "type": "datasource", "query": "prometheus", "label": "Datenquelle",
         "current": {}, "hide": 0},
        {"name": "device", "type": "query", "label": "Gerät", "datasource": DS,
         "query": {"query": "label_values(sfd_feature_value, device)", "refId": "A"},
         "definition": "label_values(sfd_feature_value, device)", "refresh": 2, "current": {}},
        {"name": "metric", "type": "query", "label": "Merkmal", "datasource": DS,
         "query": {"query": 'label_values(sfd_feature_value{device="$device"}, metric)', "refId": "B"},
         "definition": 'label_values(sfd_feature_value{device="$device"}, metric)', "refresh": 2, "current": {}},
    ]},
    "panels": panels,
}
Path(__file__).with_name("stromfresser-detektiv.json").write_text(json.dumps(dash, ensure_ascii=False, indent=1),
                                                                  encoding="utf-8")
print("ok", len(panels), "panels")
