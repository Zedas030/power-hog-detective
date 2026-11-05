"""English language and English config keys (the default for new installations)."""
from datetime import date, timedelta

import pytest

from conftest import Recorder, run_days

from sfd import synth
from sfd.config import load_config
from sfd.detector import Detector
from sfd.report import build, render_html, summary_text
from sfd.store import Store
from sfd.util import Clock

EN_YAML = """
language: en
home_assistant: {url: http://127.0.0.1:1, notify: notify.phone}
timezone: Europe/Berlin
prices: {import_eur_kwh: 0.30, export_eur_kwh: 0.08}
learning: {min_days: 14, window_days: 28}
detection: {stable_days: 3}
monitoring: {no_data_after: 30m, unavailable_after: 30m}
storage: {path: ":memory:"}
devices:
  fridge: {name: Fridge, type: cycle, profile: fridge, power: sensor.fridge_power, switch: switch.fridge}
  router: {name: Router, type: continuous, power: sensor.router_power}
  tv: {name: TV, type: usage, power: sensor.tv_power, max_runtime: 6h}
"""

START = date(2026, 9, 1)


@pytest.fixture
def en(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(EN_YAML, encoding="utf-8")
    cfg = load_config(p, require_token=False)
    store = Store(":memory:")
    clock = Clock(cfg.tz)
    rec = Recorder()
    yield cfg, store, clock, Detector(cfg, store, clock, notify=rec), rec
    load_config  # language is reset by the next load_config


def test_english_keys_and_types(en):
    cfg, *_ = en
    assert cfg.language == "en" and cfg.app_name == "Power Hog Detective"
    assert cfg.devices["fridge"].typ == "zyklus" and cfg.devices["router"].typ == "dauerlast"
    assert cfg.devices["tv"].max_laufzeit == 6 * 3600
    assert cfg.price_import == 0.30 and cfg.price_export == 0.08
    assert cfg.unavailable_after == 30 * 60 and cfg.stale_notify is False


def test_english_compressor_message(en):
    cfg, store, clock, det, rec = en
    s, _ = clock.day_bounds(START)
    _, e = clock.day_bounds(START + timedelta(days=19))
    store.add_samples("fridge", synth.fridge(s, e, cycle_min=20, pause_min=40, seed=7))
    s2, _ = clock.day_bounds(START + timedelta(days=20))
    _, e2 = clock.day_bounds(START + timedelta(days=25))
    store.add_samples("fridge", synth.fridge(s2, e2, cycle_min=28, pause_min=40, seed=8))
    run_days(det, clock, [START + timedelta(days=i) for i in range(25)])
    msgs = [m for m in rec.msgs if m["message"].startswith("Fridge")]
    assert len(msgs) == 1
    assert "compressor has been running" in msgs[0]["message"]
    assert "longer per cycle for 3 days" in msgs[0]["message"]
    assert "check the door, the seal or for icing" in msgs[0]["message"]
    assert msgs[0]["title"] == "Power Hog Detective: Fridge"
    det.feedback("Fridge", source="test")
    assert "accepted" in rec.msgs[-1]["message"] and rec.msgs[-1]["title"].endswith("Got it")


def test_english_runtime_and_number_format(en):
    cfg, store, clock, det, rec = en
    t0 = clock.at(START, 18)
    for k in range(0, int(6.5 * 3600), 60):
        clock.set(t0 + k)
        det.on_sample("tv", t0 + k, 90.0)
        det.live_check()
    assert any("TV has been running continuously for 6.0 h (configured limit 6.0 h)" in m for m in rec.texts())


def test_english_report(en):
    cfg, store, clock, det, rec = en
    s, e = clock.day_bounds(date(2026, 10, 1))[0], clock.day_bounds(date(2026, 10, 31))[1]
    store.add_samples("fridge", synth.fridge(s, e, step=60))
    store.add_samples("router", synth.constant(s, e, 15, step=60))
    run_days(det, clock, [date(2026, 10, 1) + timedelta(days=i) for i in range(31)], notify=False)
    rep = build(cfg, store, det, clock, 2026, 10)
    html = render_html(rep, cfg, "2026-11-01 07:00")
    assert "Power Hog report October 2026" in html and 'lang="en"' in html
    assert "Consumption and cost per device" in html and "Top 5 savings" in html
    assert summary_text(rep).startswith("October 2026: metering plugs")


def test_german_keys_still_work(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("sprache: de\ngeraete:\n  k: {name: Kühlschrank, typ: zyklus, leistung: sensor.k}\n"
                 "ueberwachung: {nicht_verfuegbar_nach: 15m}\nspeicher: {pfad: ':memory:'}\n", encoding="utf-8")
    cfg = load_config(p, require_token=False)
    assert cfg.language == "de" and cfg.app_name == "Stromfresser-Detektiv"
    assert cfg.devices["k"].typ == "zyklus" and cfg.unavailable_after == 15 * 60


def test_shipped_example_config_loads():
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "config.example.yaml", require_token=False)
    assert cfg.language == "en" and len(cfg.devices) == 9
    assert cfg.devices["fridge"].typ == "zyklus" and cfg.devices["printer_3d"].programme_pruefen is False
    assert cfg.house.hausverbrauch is None and cfg.house.solar == ["sensor.battery_output_power"]
    assert cfg.house.dle_name == "Instantaneous water heater" and cfg.db_path == "/data/power-hog.sqlite"
