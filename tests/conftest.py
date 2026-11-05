import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sfd.config import load_config  # noqa: E402
from sfd.detector import Detector  # noqa: E402
from sfd.store import Store  # noqa: E402
from sfd.util import Clock  # noqa: E402

BASE_YAML = """
language: de
home_assistant:
  url: http://127.0.0.1:1
  notify: notify.test
zeitzone: Europe/Berlin
preise: {bezug_eur_kwh: 0.34, einspeisung_eur_kwh: 0.0}
lernen: {min_tage: 14, fenster_tage: 28, max_ausschluss_tage: 14, min_tage_nach_feedback: 3}
erkennung: {stabil_tage: 3, rel_schwelle: 0.25, z_schwelle: 3.0, normal_tage_bis_entwarnung: 2}
ueberwachung: {keine_daten_nach: 30m, keine_daten_melden: true, nicht_verfuegbar_nach: 15m, hausdaten_weg_nach: 30m}
speicher: {pfad: ":memory:"}
haus:
  hausverbrauch: sensor.haus
  netzbezug: sensor.netz
  nacht_von: "01:00"
  nacht_bis: "05:00"
geraete:
  kuehlschrank: {name: Kühlschrank, typ: zyklus, leistung: sensor.kuehl, schalter: switch.kuehl}
  router: {name: Router, typ: dauerlast, leistung: sensor.router, schalter: switch.router}
  spuelmaschine: {name: Spülmaschine, typ: programm, leistung: sensor.spuel}
  tv: {name: TV, typ: nutzung, leistung: sensor.tv, max_laufzeit: 6h}
  zelt: {name: Zelt, typ: zeitplan, leistung: sensor.zelt}
"""


class Recorder:
    def __init__(self):
        self.msgs = []

    def __call__(self, title, message, action, tag):
        self.msgs.append({"title": title, "message": message, "action": action, "tag": tag})

    def texts(self):
        return [m["message"] for m in self.msgs]


@pytest.fixture
def env(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(BASE_YAML, encoding="utf-8")
    cfg = load_config(p, require_token=False)
    store = Store(":memory:")
    clock = Clock(cfg.tz)
    rec = Recorder()
    det = Detector(cfg, store, clock, notify=rec)
    return cfg, store, clock, det, rec


START = date(2026, 9, 1)


def days(n, start=START):
    return [start + timedelta(days=i) for i in range(n)]


def run_days(det, clock, day_list, notify=True):
    """Process + evaluate each day in order, with the clock set to 00:20 of the next day."""
    for d in day_list:
        nxt = d + timedelta(days=1)
        clock.set(clock.at(nxt, 0, 20))
        det.process_day(d, aggregates=False)
        det.evaluate_day(d, notify=notify)


@pytest.fixture
def env_default(tmp_path):
    """Config with the shipped default for 'no data' alarms (not pushed)."""
    p = tmp_path / "config.yaml"
    p.write_text(BASE_YAML.replace("keine_daten_melden: true, ", "").replace("nicht_verfuegbar_nach: 15m, ", ""), encoding="utf-8")
    cfg = load_config(p, require_token=False)
    store = Store(":memory:")
    clock = Clock(cfg.tz)
    rec = Recorder()
    return cfg, store, clock, Detector(cfg, store, clock, notify=rec), rec
