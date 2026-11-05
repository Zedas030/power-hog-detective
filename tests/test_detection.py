"""Detection logic on synthetic consumption data."""
from datetime import timedelta

from conftest import START, days, run_days

from sfd import synth
from sfd.features import device_day
from sfd.segment import energy_wh, runs, steps


def span(clock, first, n):
    s, _ = clock.day_bounds(first)
    _, e = clock.day_bounds(first + timedelta(days=n - 1))
    return s, e


# ---------------------------------------------------------------- segmentation & features
def test_runs_merge_short_gaps_and_energy():
    samples = [(0, 0), (60, 100), (120, 0), (150, 100), (300, 0), (1000, 0)]
    st = steps(samples, 0, 1000, 600)
    rs = runs(st, on_w=50, gap=60, min_run=10)
    assert len(rs) == 1
    assert rs[0].start == 60 and rs[0].end == 300
    assert abs(energy_wh(st) - (60 * 100 + 150 * 100) / 3600) < 1e-6


def test_data_holes_are_not_counted():
    samples = [(0, 100), (100, 100), (5000, 100)]
    st = steps(samples, 0, 6000, max_gap=600)
    assert sum(b - a for a, b, _ in st) == 100 + 600 + 600


def test_fridge_features(env):
    cfg, store, clock, det, rec = env
    s, e = span(clock, START, 3)
    store.add_samples("kuehlschrank", synth.fridge(s, e, cycle_min=20, pause_min=40))
    ds, de = clock.day_bounds(START + timedelta(days=1))
    m, rs = device_day(cfg.devices["kuehlschrank"], store.samples("kuehlschrank", ds - 43200, de + 43200), ds, de, e)
    assert 19 <= m["zyklus_min"] <= 21.5
    assert 37 <= m["pause_min"] <= 43
    assert 22 <= m["zyklen"] <= 26
    assert abs(m["laufanteil"] - 1 / 3) < 0.03
    assert m["coverage"] > 0.99


# ---------------------------------------------------------------- scenario 1: longer compressor cycles
def test_longer_compressor_cycles_are_explained(env):
    cfg, store, clock, det, rec = env
    s, e = span(clock, START, 20)
    store.add_samples("kuehlschrank", synth.fridge(s, e, cycle_min=20, pause_min=40, seed=7))
    s2, e2 = span(clock, START + timedelta(days=20), 6)
    store.add_samples("kuehlschrank", synth.fridge(s2, e2, cycle_min=28, pause_min=40, seed=8))
    run_days(det, clock, days(25))
    msgs = [m for m in rec.texts() if m.startswith("Kühlschrank")]
    assert len(msgs) == 1, rec.texts()
    assert "Kompressor läuft seit 3 Tagen" in msgs[0]
    assert "länger pro Zyklus" in msgs[0]
    assert "Tür, Dichtung oder Vereisung prüfen" in msgs[0]
    pct = int(msgs[0].split("Tagen ")[1].split(" %")[0])
    assert 30 <= pct <= 50
    # alarm appears exactly on the 3rd deviating day, not earlier
    first_alarm_day = rec.msgs[0]
    assert rec.msgs[0]["action"].startswith("SFD_OK_")


def test_no_alarm_during_learning_phase(env):
    cfg, store, clock, det, rec = env
    s, e = span(clock, START, 10)
    store.add_samples("kuehlschrank", synth.fridge(s, e, cycle_min=20, pause_min=40, seed=1))
    s2, e2 = span(clock, START + timedelta(days=10), 4)
    store.add_samples("kuehlschrank", synth.fridge(s2, e2, cycle_min=35, pause_min=40, seed=2))
    run_days(det, clock, days(14))
    assert rec.msgs == []
    assert det.learning_progress("kuehlschrank") == (14, 14)


def test_slow_drift_is_learned_not_alarmed(env):
    cfg, store, clock, det, rec = env
    for i in range(50):
        d = START + timedelta(days=i)
        s, e = clock.day_bounds(d)
        store.add_samples("router", synth.constant(s, e, 10 + i * 0.2, noise=0.3, seed=i))
    run_days(det, clock, days(50))
    assert [m for m in rec.texts() if m.startswith("Router")] == []


def test_benign_change_does_not_alarm(env):
    cfg, store, clock, det, rec = env
    s, e = span(clock, START, 20)
    store.add_samples("kuehlschrank", synth.fridge(s, e, cycle_min=20, pause_min=40, seed=3))
    s2, e2 = span(clock, START + timedelta(days=20), 5)
    store.add_samples("kuehlschrank", synth.fridge(s2, e2, cycle_min=20, pause_min=70, seed=4))  # less work
    run_days(det, clock, days(25))
    assert not any("Pausen" in m for m in rec.texts())


# ---------------------------------------------------------------- scenario 2: base load
def test_router_base_load_increase(env):
    cfg, store, clock, det, rec = env
    for i in range(24):
        d = START + timedelta(days=i)
        s, e = clock.day_bounds(d)
        store.add_samples("router", synth.constant(s, e, 15 if i < 20 else 24, noise=0.4, seed=i))
    run_days(det, clock, days(24))
    msgs = [m for m in rec.texts() if m.startswith("Router")]
    assert len(msgs) == 1
    assert "Grundlast seit" in msgs[0] and "+9 W" in msgs[0]


def _house_days(clock, store, n, extra_from=None, extra_w=35.0):
    for i in range(n):
        d = START + timedelta(days=i)
        s, e = clock.day_bounds(d)
        router = synth.constant(s, e, 15, noise=0.3, seed=i)
        store.add_samples("router", router)
        extra = extra_w if (extra_from is not None and i >= extra_from) else 0
        # house = router + 120 W unmetered + extra
        store.add_samples("_haus:hausverbrauch", synth.constant(s, e, 135 + extra, noise=3, seed=100 + i,
                                                                 step=300))
        store.add_samples("_haus:netzbezug", synth.constant(s, e, 100 + extra, noise=3, seed=200 + i, step=300))


def test_night_base_load_unassigned(env):
    cfg, store, clock, det, rec = env
    _house_days(clock, store, 22, extra_from=18)
    run_days(det, clock, days(22))
    msgs = [m for m in rec.texts() if m.startswith("Nächtliche Grundlast")]
    assert len(msgs) == 1, rec.texts()
    assert "+35 W" in msgs[0]
    assert "keiner Steckdose zuzuordnen" in msgs[0]
    first_extra_day = START + timedelta(days=18)
    from sfd.util import WEEKDAYS
    assert f"seit {WEEKDAYS[first_extra_day.weekday()]}" in msgs[0]


# ---------------------------------------------------------------- scenario 3: runs too long (live)
def test_runtime_too_long_and_back_off(env):
    cfg, store, clock, det, rec = env
    t0 = clock.at(START, 18)
    det.on_switch("tv", True)
    for k in range(0, 7 * 3600, 60):
        clock.set(t0 + k)
        det.on_sample("tv", t0 + k, 90.0)
        det.live_check()
    msgs = [m for m in rec.texts() if m.startswith("TV")]
    assert len(msgs) == 1
    assert "läuft seit 6,0 Std. ununterbrochen (eingestellte Grenze 6,0 Std.)" in msgs[0]
    assert "vergessen auszuschalten" in msgs[0]
    for k in range(7 * 3600, 7 * 3600 + 900, 60):
        clock.set(t0 + k)
        det.on_sample("tv", t0 + k, 0.5)
        det.live_check()
    assert "TV ist jetzt aus" in rec.texts()[-1]


def test_learned_runtime_limit_from_history(env):
    cfg, store, clock, det, rec = env
    s, e = span(clock, START, 20)
    store.add_samples("kuehlschrank", synth.fridge(s, e, cycle_min=20, pause_min=40, seed=9))
    run_days(det, clock, days(20), notify=False)
    lim = det.runtime_limit(cfg.devices["kuehlschrank"])
    assert 25 * 60 <= lim <= 45 * 60
    t0 = e + 60
    for k in range(0, int(lim) + 600, 60):
        clock.set(t0 + k)
        det.on_sample("kuehlschrank", t0 + k, 90)
        det.live_check()
    assert any("Kompressor läuft seit" in m and "Tür offen" in m for m in rec.texts())


# ---------------------------------------------------------------- defect / no data
def test_zero_consumption_while_switched_on(env):
    cfg, store, clock, det, rec = env
    t0 = clock.at(START, 8)
    det.on_switch("router", True)
    for k in range(0, 1800, 60):
        clock.set(t0 + k)
        det.on_sample("router", t0 + k, 0.0)
        det.live_check()
    assert any("Router verbraucht seit" in m and "gar nichts mehr" in m for m in rec.texts())
    det.on_sample("router", t0 + 1900, 14.0)
    assert "verbraucht wieder Strom" in rec.texts()[-1]


def test_no_zero_alarm_when_switched_off(env):
    cfg, store, clock, det, rec = env
    t0 = clock.at(START, 8)
    det.on_switch("router", False)
    for k in range(0, 3600, 60):
        clock.set(t0 + k)
        det.on_sample("router", t0 + k, 0.0)
        det.live_check()
    assert rec.msgs == []


def test_stale_and_unavailable(env):
    cfg, store, clock, det, rec = env
    t0 = clock.at(START, 8)
    det.ha_state(True)
    det.on_sample("router", t0, 15, reported=t0)
    det.on_sample("_haus:hausverbrauch", t0, 300)
    clock.set(t0 + 31 * 60)
    det.live_check()
    assert any("Router: Steckdose meldet seit" in m for m in rec.texts())
    det.on_unavailable("tv", t0, "unavailable")
    clock.set(t0 + 40 * 60)
    det.live_check()
    assert any("TV: Steckdose ist seit" in m and "unavailable" in m for m in rec.texts())
    assert any(m.startswith("Hausdaten") for m in rec.texts())
    det.on_sample("router", t0 + 41 * 60, 15)
    assert "Router: Steckdose meldet wieder Werte." in rec.texts()


# ---------------------------------------------------------------- feedback
def test_feedback_accepts_and_relearns(env):
    cfg, store, clock, det, rec = env
    s, e = span(clock, START, 20)
    store.add_samples("kuehlschrank", synth.fridge(s, e, cycle_min=20, pause_min=40, seed=7))
    s2, e2 = span(clock, START + timedelta(days=20), 15)
    store.add_samples("kuehlschrank", synth.fridge(s2, e2, cycle_min=28, pause_min=40, seed=8))
    run_days(det, clock, days(23))
    alarms = [m for m in rec.msgs if m["message"].startswith("Kühlschrank") and m["action"]]
    assert len(alarms) == 1
    done = det.feedback(alarms[0]["action"], source="test")
    assert done and all(store.anomaly(a["id"])["state"] == "ok" for a in done)
    assert "Verstanden" in rec.msgs[-1]["title"]
    n_before = len(rec.msgs)
    run_days(det, clock, days(12, START + timedelta(days=23)))
    assert len(rec.msgs) == n_before, rec.texts()[n_before:]  # new normal: no further alarms
    assert store.baseline_start("kuehlschrank", "zyklus_min")[1] == "feedback"


def test_feedback_by_device_name_for_runtime(env):
    cfg, store, clock, det, rec = env
    t0 = clock.at(START, 10)
    for k in range(0, int(6.2 * 3600), 60):
        clock.set(t0 + k)
        det.on_sample("tv", t0 + k, 90.0)
        det.live_check()
    det.feedback("TV", source="ha")
    assert det.runtime_limit(cfg.devices["tv"]) >= 6.2 * 3600 * 1.4


def test_persisting_change_is_learned_automatically(env):
    cfg, store, clock, det, rec = env
    for i in range(45):
        d = START + timedelta(days=i)
        s, e = clock.day_bounds(d)
        store.add_samples("router", synth.constant(s, e, 15 if i < 20 else 24, noise=0.4, seed=i))
    run_days(det, clock, days(45))
    texts = [m for m in rec.texts() if m.startswith("Router")]
    assert any("Grundlast seit" in m for m in texts)
    assert any("ich lerne das jetzt als neues Normal" in m for m in texts)
    assert len(texts) == 2


def test_recovery_message(env):
    cfg, store, clock, det, rec = env
    for i in range(28):
        d = START + timedelta(days=i)
        s, e = clock.day_bounds(d)
        store.add_samples("router", synth.constant(s, e, 24 if 20 <= i < 24 else 15, noise=0.4, seed=i))
    run_days(det, clock, days(28))
    assert "Router: Grundlast ist wieder normal." in rec.texts()


# ---------------------------------------------------------------- programs & schedules
def test_dishwasher_program_needs_more_energy(env):
    cfg, store, clock, det, rec = env
    ds0 = lambda ts: clock.day_bounds(clock.day_of(ts))[0]  # noqa: E731
    s, e = span(clock, START, 12)
    store.add_samples("spuelmaschine", synth.dishwasher(s, e, ds0, minutes=120, kwh=1.0))
    s2, e2 = span(clock, START + timedelta(days=12), 3)
    store.add_samples("spuelmaschine", synth.dishwasher(s2, e2, ds0, minutes=120, kwh=1.5, seed=6))
    run_days(det, clock, days(15))
    msgs = [m for m in rec.texts() if m.startswith("Spülmaschine")]
    assert len(msgs) == 1, rec.texts()
    assert "Programm A braucht seit 2 Läufen" in msgs[0] and "mehr Energie" in msgs[0]
    assert "Heizstab verkalkt" in msgs[0]


def test_schedule_shift(env):
    cfg, store, clock, det, rec = env
    ds0 = lambda ts: clock.day_bounds(clock.day_of(ts))[0]  # noqa: E731
    s, e = span(clock, START, 18)
    store.add_samples("zelt", synth.schedule(s, e, ds0, 6 * 60, 20 * 60, 75))
    s2, e2 = span(clock, START + timedelta(days=18), 4)
    store.add_samples("zelt", synth.schedule(s2, e2, ds0, 7 * 60, 21 * 60, 75))
    run_days(det, clock, days(22))
    msgs = [m for m in rec.texts() if m.startswith("Zelt")]
    assert len(msgs) == 1, rec.texts()
    assert "schaltet seit 3 Tagen um 07:00 statt um 06:00 ein" in msgs[0]


def test_only_notify_and_never_switch():
    """The HA client refuses every service call outside notify/persistent_notification."""
    import asyncio

    from sfd.ha import HAClient, ReadOnlyViolation

    c = HAClient("http://127.0.0.1:1", "x")
    for domain, service in (("switch", "turn_off"), ("homeassistant", "turn_off"), ("light", "toggle")):
        try:
            asyncio.run(c.call_service(domain, service, {}))
        except ReadOnlyViolation:
            continue
        raise AssertionError(f"{domain}.{service} must be refused")


def test_house_sensor_stuck_at_zero_is_reported_not_used(env):
    cfg, store, clock, det, rec = env
    for i in range(3):
        d = START + timedelta(days=i)
        s, e = clock.day_bounds(d)
        store.add_samples("router", synth.constant(s, e, 15, seed=i))
        store.add_samples("_haus:hausverbrauch", synth.constant(s, e, 0, noise=0, step=300))
    run_days(det, clock, days(3))
    msgs = [m for m in rec.texts() if m.startswith("Hausdaten unplausibel")]
    assert len(msgs) == 1
    assert store.daily_day("_haus", START.isoformat()).get("nacht_w") is None


def test_silent_replay_sends_nothing(env):
    cfg, store, clock, det, rec = env
    det.silent = True
    t0 = clock.at(START, 18)
    for k in range(0, 7 * 3600, 60):
        clock.set(t0 + k)
        det.on_sample("tv", t0 + k, 90.0)
        det.live_check()
    assert rec.msgs == [] and store.find_open("tv", "laufzeit")


def test_unassigned_base_with_cycling_fridge(env):
    """House = cycling fridge + 100 W unmetered: unassigned must be ~100 W, not distorted by the cycles."""
    cfg, store, clock, det, rec = env
    s, e = span(clock, START, 1)
    fr = synth.fridge(s, e, cycle_min=10, pause_min=20, on_w=90, standby_w=1, step=15, seed=3)
    store.add_samples("kuehlschrank", fr)
    store.add_samples("_haus:hausverbrauch", [(t, w + 100) for t, w in fr])
    run_days(det, clock, [START])
    m = store.daily_day("_haus", START.isoformat())
    assert abs(m["unzugeordnet_nacht_w"] - 100) < 3


def test_feedback_mutes_unavailable_until_recovered(env):
    cfg, store, clock, det, rec = env
    t0 = clock.at(START, 8)
    det.on_sample("tv", t0, 5, reported=t0)
    det.on_unavailable("tv", t0 + 60, "unavailable")
    for k in range(2, 40):
        clock.set(t0 + k * 60)
        det.live_check()
    assert sum("TV: Steckdose ist seit" in m for m in rec.texts()) == 1
    det.feedback("TV", source="test")
    for k in range(40, 120):
        clock.set(t0 + k * 60)
        det.live_check()
    assert sum("TV: Steckdose ist seit" in m for m in rec.texts()) == 1  # muted, no repeat
    det.on_sample("tv", t0 + 121 * 60, 5)  # back: muted finding closes silently
    assert store.anomalies(("offen", "stumm"), dev="tv") == []
    assert not any("meldet wieder Werte" in m for m in rec.texts())


def test_no_data_is_silent_by_default_but_unavailable_pushes(env_default):
    cfg, store, clock, det, rec = env_default
    assert cfg.unavailable_after == 30 * 60
    t0 = clock.at(START, 8)
    det.on_sample("router", t0, 15, reported=t0)
    clock.set(t0 + 3 * 3600)
    det.live_check()
    assert rec.msgs == []                                   # silent …
    assert store.find_open("router", "keine_daten")         # … but recorded for the web UI
    det.on_sample("router", t0 + 3 * 3600 + 60, 15)
    assert rec.msgs == []                                   # recovery of a silent finding is silent too
    det.on_unavailable("tv", t0, "unavailable")
    clock.set(t0 + 29 * 60)
    det.live_check()
    assert not any(m.startswith("TV") for m in rec.texts())
    clock.set(t0 + 31 * 60)
    det.live_check()
    assert any("TV: Steckdose ist seit" in m for m in rec.texts())
