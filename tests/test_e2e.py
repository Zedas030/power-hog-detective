"""End-to-end: the real service against a fake Home Assistant (WebSocket + REST), real web server, real SQLite."""
import asyncio
import socket
import time

import aiohttp

from fake_ha import TOKEN, FakeHA, wait_for

from sfd.app import Service
from sfd.config import load_config

CFG = """
home_assistant: {{url: "{url}", notify: notify.mobile_app_test}}
zeitzone: Europe/Berlin
prometheus: {{url: null}}
web: {{port: {port}}}
lernen: {{min_tage: 14}}
ueberwachung:
  keine_daten_nach: 4s
  nicht_verfuegbar_nach: 2s
  ha_weg_nach: 1s
  hausdaten_weg_nach: 3s
  abfrageintervall: 1s
  registry_pruefung: 1s
speicher: {{pfad: "{db}", schreibintervall: 1s}}
haus:
  hausverbrauch: sensor.haus
geraete:
  tv: {{name: TV, typ: nutzung, leistung: sensor.tv, schalter: switch.tv, max_laufzeit: 3s, luecke: 2s, min_lauf: 1s}}
  router: {{name: Router, typ: dauerlast, leistung: sensor.router, schalter: switch.router}}
  kuehlschrank: {{name: Kühlschrank, typ: zyklus, leistung: sensor.kuehl}}
  boiler: {{name: Boiler, typ: zyklus, leistung: sensor.boiler, schalter: switch.boiler}}
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def msgs(ha):
    return [n["message"] for n in ha.notifications()]


def test_end_to_end(tmp_path, monkeypatch):
    asyncio.run(_scenario(tmp_path, monkeypatch))


async def _scenario(tmp_path, monkeypatch):
    ha = FakeHA()
    url = await ha.start()
    port = free_port()
    ha.set("sensor.tv", 0.4, "W")
    ha.set("switch.tv", "on")
    ha.set("sensor.router", 14.8, "W", unique_id="router-power-uid")
    ha.set("switch.router", "on")
    ha.set("sensor.kuehl", 80, "W")
    ha.set("sensor.haus", 400, "W", platform="growatt_server")
    # switched-off plug: power stays 0 and HA does not refresh its last_reported, but the voltage sensor of the
    # same device keeps reporting -> must NOT be reported as 'no values'
    ha.set("sensor.boiler", 0.0, "W", reported=time.time() - 7200, device_id="dev-boiler")
    ha.set("sensor.boiler_spannung", 231, "V", device_id="dev-boiler")
    ha.set("switch.boiler", "off", device_id="dev-boiler")
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(CFG.format(url=url, port=port, db=(tmp_path / "db.sqlite").as_posix()), encoding="utf-8")
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    cfg = load_config(cfgp)
    cfg.report_dir = str(tmp_path / "berichte")
    svc = Service(cfg)
    svc.check_every = svc.deliver_every = svc.publish_every = 0.3
    svc.schedule_every = 5
    svc.reconnect_min = 0.3
    task = asyncio.create_task(svc.run())
    try:
        await wait_for(lambda: svc.det.ha_connected)
        # 1) the 'war ok' script is created in HA, own sensors are published
        await wait_for(lambda: "stromfresser_war_ok" in ha.scripts)
        assert ha.scripts["stromfresser_war_ok"]["sequence"][0]["event"] == "stromfresser_feedback"
        await wait_for(lambda: "sensor.stromfresser_status" in ha.posted_states)
        await wait_for(lambda: "sensor.stromfresser_tv" in ha.posted_states)  # sensors are posted one by one

        # 2) TV runs longer than its configured limit -> plain-language message with "War ok" button
        await ha.change("sensor.tv", 95)
        for _ in range(12):
            await asyncio.sleep(0.4)
            await ha.change("sensor.tv", 95 + _ % 2)
        n = await wait_for(lambda: [x for x in ha.notifications() if "TV läuft seit" in x["message"]])
        assert "vergessen auszuschalten" in n[0]["message"]
        action = n[0]["data"]["actions"][0]["action"]
        assert action.startswith("SFD_OK_") and n[0]["data"]["actions"][0]["title"] == "War ok"

        # 3) user taps "War ok" on the phone -> anomaly closed, limit raised, confirmation sent
        await ha.fire("mobile_app_notification_action", {"action": action})
        await wait_for(lambda: any("Verstanden" in x.get("title", "") for x in ha.notifications()))
        aid = int(action.split("_")[-1])
        assert svc.store.anomaly(aid)["state"] == "ok"
        assert svc.det.runtime_limit(cfg.devices["tv"]) > 3
        assert svc.store.feedback_list()[0][3] == "handy"

        # 4) router entity renamed in HA -> detected via unique_id, followed automatically
        ha.rename("sensor.router", "sensor.router_neu")
        await wait_for(lambda: any("Entität umbenannt" in m for m in msgs(ha)))
        assert any("sensor.router heißt jetzt sensor.router_neu" in m for m in msgs(ha))
        assert "sensor.router_neu" in svc.entity_map
        await ha.change("sensor.router_neu", 16.0)
        await wait_for(lambda: svc.det.live["router"].w == 16.0)

        # 5) fridge plug stops reporting (HA last_reported frozen) -> recorded for the web UI, but NOT pushed
        #    (default keine_daten_melden: false – rarely used plugs would nag); only 'unavailable' is pushed
        ha.set("sensor.kuehl", 80, "W", reported=time.time() - 3600)
        await wait_for(lambda: svc.store.find_open("kuehlschrank", "keine_daten"), timeout=40)
        assert not any("Kühlschrank: Steckdose meldet seit" in m for m in msgs(ha))

        assert svc.alive_map.get("sensor.boiler_spannung") == "boiler"
        assert not any(m.startswith("Boiler") for m in msgs(ha)), msgs(ha)

        # 6) house data entity disappears (Growatt disabled) -> self-monitoring message
        ha.states.pop("sensor.haus")
        await wait_for(lambda: any(m.startswith("Hausdaten") for m in msgs(ha)), timeout=40)

        # 7) feedback through the HA script/event (device name) mutes the open house finding
        await ha.fire("stromfresser_feedback", {"geraet": "Haus"})
        await wait_for(lambda: svc.store.anomalies(("stumm",), dev="_haus"))

        # 8) TV plug becomes unavailable -> message after 'nicht_verfuegbar_nach'
        await ha.change("sensor.tv", "unavailable")
        await wait_for(lambda: any("TV: Steckdose ist seit" in m for m in msgs(ha)), timeout=40)

        # 9) web UI, health, metrics
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/health") as r:
                assert (await r.json())["ha_connected"] is True
            async with s.get(f"http://127.0.0.1:{port}/metrics") as r:
                text = await r.text()
                assert "sfd_device_power_watts" in text and "sfd_ha_connected 1" in text
            async with s.get(f"http://127.0.0.1:{port}/") as r:
                assert "Stromfresser-Detektiv" in await r.text()

        # 10) HA goes away -> after reconnect a message about the outage (queued while HA was down)
        ha.accept = False
        await ha.drop_clients()
        await wait_for(lambda: not svc.det.ha_connected)
        await asyncio.sleep(1.5)
        ha.accept = True
        await wait_for(lambda: any("Home Assistant war" in m for m in msgs(ha)), timeout=40)

        # 11) samples were persisted to SQLite
        svc.store.flush()
        assert svc.store.x("SELECT COUNT(*) FROM samples WHERE dev='tv'").fetchone()[0] > 5

        # read-only guarantee: only notify services were called
        assert {d for d, _s, _ in ha.service_calls} == {"notify"}
    finally:
        svc.stop.set()
        await asyncio.wait_for(task, 10)
        await ha.stop()
