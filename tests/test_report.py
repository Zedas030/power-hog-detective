"""Monthly report on synthetic data (no house meter, solar estimated from battery output)."""
from datetime import date, timedelta

from conftest import run_days

from sfd import synth
from sfd.report import build, render_html, summary_text


def test_monthly_report_without_meter(env, tmp_path):
    cfg, store, clock, det, rec = env
    cfg.house.hausverbrauch = None
    cfg.house.netzbezug = None
    cfg.house.solar = ["sensor.noah"]
    first = date(2026, 10, 1)
    ds0 = lambda ts: clock.day_bounds(clock.day_of(ts))[0]  # noqa: E731
    s, e = clock.day_bounds(first)[0], clock.day_bounds(date(2026, 10, 31))[1]
    store.add_samples("kuehlschrank", synth.fridge(s, e, cycle_min=20, pause_min=30, on_w=90, step=30))
    store.add_samples("router", synth.constant(s, e, 15, step=60))
    store.add_samples("tv", synth.runs(s, e, [(s + i * 86400 + 19 * 3600, s + i * 86400 + 22 * 3600) for i in range(31)],
                                       on_w=80, off_w=6, step=60))
    store.add_samples("spuelmaschine", synth.dishwasher(s, e, ds0, at_min=19 * 60, kwh=1.0, step=60))
    # solar output into the house: 300 W between 10 and 16 h
    solar = [(int(s + i * 3600), 300.0 if 10 <= clock.local(s + i * 3600).hour < 16 else 0.0) for i in range(31 * 24)]
    store.put_energy("_solar:sensor.noah", solar, "statistik")
    run_days(det, clock, [first + timedelta(days=i) for i in range(31)], notify=False)
    rep = build(cfg, store, det, clock, 2026, 10)
    names = [r["name"] for r in rep["geraete"]]
    assert set(names[:2]) == {"Kühlschrank", "Spülmaschine"}
    fridge = next(r for r in rep["geraete"] if r["name"] == "Kühlschrank")
    assert 25 < fridge["kwh"] < 50  # 90 W * 40 % * 744 h ≈ 27 kWh + standby
    assert rep["haus_kwh"] is None and rep["solar_geschaetzt"] is True
    assert 0.05 < rep["solar_anteil"] < 0.6
    assert len(rep["sparpotenziale"]) >= 2
    titles = " ".join(x["titel"] for x in rep["sparpotenziale"])
    assert "TV: Standby abschalten" in titles
    assert "Spülmaschine: in Sonnenstunden verlegen" in titles
    assert all(x["eur"] > 0 for x in rep["sparpotenziale"])
    html = render_html(rep, cfg, "01.11.2026 07:00")
    assert "Stromfresser-Bericht Oktober 2026" in html and "Kein Hauszähler" in html
    assert "Grundlast der Messsteckdosen" in html
    txt = summary_text(rep)
    assert txt.startswith("Oktober 2026:") and "Größtes Sparpotenzial" in txt


def test_metrics_have_no_duplicate_series(env):
    from sfd.metrics import render
    cfg, store, clock, det, rec = env
    s, e = clock.day_bounds(date(2026, 10, 1))[0], clock.day_bounds(date(2026, 10, 20))[1]
    store.add_samples("kuehlschrank", synth.fridge(s, e, step=60))
    run_days(det, clock, [date(2026, 10, 1) + timedelta(days=i) for i in range(20)], notify=False)
    lines = [ln.rsplit(" ", 1)[0] for ln in render(det).splitlines() if ln and not ln.startswith("#")]
    assert len(lines) == len(set(lines))
    assert any(ln.startswith("sfd_feature_baseline") for ln in lines)
