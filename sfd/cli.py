"""Command line: run the service, pre-learn, give feedback, build reports, run acceptance scenarios."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import timedelta

from .config import TYPE_LABEL, load_config
from .detector import HOUSE, OPEN_STATES, Detector
from .notifier import Notifier
from .store import Store
from .i18n import L, fmt_datetime, fmt_num
from .util import Clock

DEFAULT_CONFIG = os.environ.get("SFD_CONFIG", "/config/config.yaml")
DEFAULT_ENV = os.environ.get("SFD_ENV", "/config/.env")


def _offline(args, require_token=False):
    cfg = load_config(args.config, args.env, require_token=require_token)
    clock = Clock(cfg.tz)
    store = Store(cfg.db_path)
    notifier = Notifier(cfg, store, clock)  # queues into the outbox, the running service delivers
    det = Detector(cfg, store, clock, notify=notifier)
    return cfg, clock, store, det, notifier


def cmd_run(args):
    from .app import Service
    cfg = load_config(args.config, args.env)
    asyncio.run(Service(cfg).run())


def cmd_status(args):
    cfg, clock, store, det, _ = _offline(args)
    print(f"{L('Database', 'Datenbank')}: {cfg.db_path} ({fmt_num(store.size_bytes() / 1e6, 1)} MB)")
    print(f"{L('Pre-learned', 'Vorgelernt')}: {store.meta('vorgelernt')}")
    print(f"{L('Last daily evaluation', 'Letzte Tagesauswertung')}: {store.meta('last_daily')}")
    yesterday = (clock.local().date() - timedelta(days=1)).isoformat()
    print("\n" + L("Device                    Type             Learned   yesterday",
                   "Gerät                     Typ              Lerntage  gestern"))
    for key, typ in det.targets():
        n, m = det.learning_progress(key)
        name = det.name(key)
        f = store.daily_day(key, yesterday)
        show = {k: v for k, v in f.items() if k in ("kwh", "zyklus_min", "pause_min", "zyklen", "grundlast_w",
                                                     "standby_w", "ein_min", "betrieb_w", "nacht_w",
                                                     "unzugeordnet_nacht_w", "laeufe")}
        label = TYPE_LABEL.get(typ, L("House", "Haus"))
        print(f"{name[:25]:<25} {label[:16]:<16} {n:>2}/{m:<2}     {json.dumps(show, ensure_ascii=False)}")
    print("\n" + L("Open findings:", "Offene Auffälligkeiten:"))
    rows = store.anomalies(OPEN_STATES, test=0)
    for a in rows:
        print(f"  [{a['id']}] {a['state']}: {a['message']}")
    if not rows:
        print("  " + L("none", "keine"))


def cmd_ok(args):
    cfg, clock, store, det, _ = _offline(args)
    try:
        done = det.feedback(args.ziel, source="cli", note=args.notiz)
    except KeyError as e:
        sys.exit(str(e))
    if not done:
        print(L("Nothing open found.", "Nichts Offenes gefunden."))
    for a in done:
        print(f"{L('Was ok', 'War ok')}: [{a['id']}] {a['message']}")


def cmd_list(args):
    cfg, clock, store, det, _ = _offline(args)
    for a in store.recent_anomalies(args.anzahl):
        when = fmt_datetime(clock.local(a["opened"])) if a["opened"] else "?"
        print(f"[{a['id']}] {when} {a['state']:<9} {'TEST ' if a['test'] else ''}{a['message']}")


def cmd_prelearn(args):
    from .ha import HAClient
    from .history import prelearn
    cfg = load_config(args.config, args.env)
    clock = Clock(cfg.tz)
    store = Store(cfg.db_path)
    notifier = Notifier(cfg, store, clock)
    det = Detector(cfg, store, clock, notify=notifier)

    async def go():
        ha = HAClient(cfg.ha_url, cfg.ha_token)
        await ha.ws_connect()
        reader = asyncio.create_task(ha.ws_loop())
        friendly = {}
        for d in cfg.devices.values():
            st = await ha.get_state(d.leistung)
            if st:
                friendly[d.leistung] = st["attributes"].get("friendly_name")
        print(L(f"Pre-learning from {args.tage or cfg.prelearn_days} days …",
            f"Vorlernen über {args.tage or cfg.prelearn_days} Tage …"))
        rep = await prelearn(cfg, store, det, clock, ha=ha, days=args.tage, friendly_names=friendly)
        reader.cancel()
        await ha.close()
        return rep

    rep = asyncio.run(go())
    store.set_meta("last_daily", (clock.local().date() - timedelta(days=1)).isoformat())
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    open_ = store.anomalies(("offen",), test=0)
    print(L(f"\nDone. Open findings from the history: {len(open_)}",
            f"\nFertig. Offene Auffälligkeiten aus der Historie: {len(open_)}"))
    for a in open_:
        print(f"  [{a['id']}] {a['message']}")
    if args.melden:
        days = args.tage or cfg.prelearn_days
        lines = [L(f"Pre-learning finished ({days} days of history).", f"Vorlernen abgeschlossen ({days} Tage Historie).")]
        ready = [det.name(k) for k, _ in det.targets() if det.learning_progress(k)[0] >= cfg.min_days]
        lines.append(L("Fully learned: ", "Fertig gelernt: ") + (", ".join(ready) or L("none yet", "noch keins")) + ".")
        if open_:
            lines.append(L("Already unusual: ", "Schon jetzt auffällig: ") + " | ".join(a["message"] for a in open_[:5]))
        notifier(f"{cfg.app_name}: " + L("learning phase", "Lernphase"), " ".join(lines),
                 ("SFD_OK_" + "_".join(str(a["id"]) for a in open_)) if open_ else None, "sfd-start")
        for a in open_:
            store.update_anomaly(a["id"], notified=clock.now())


def cmd_report(args):
    from .report import eur, report_title, summary_text, write_report
    cfg, clock, store, det, notifier = _offline(args)
    if args.monat:
        y, m = (int(x) for x in args.monat.split("-"))
    else:
        now = clock.local()
        y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    path, rep = write_report(cfg, store, det, clock, y, m)
    print(f"{L('Report', 'Bericht')}: {path}")
    print(summary_text(rep))
    for s in rep["sparpotenziale"]:
        print(f"  - {s['titel']}: {eur(s['eur'])}/" + L("year", "Jahr"))
    if args.senden:
        adr = (cfg.raw.get("web") or {}).get("adresse", "localhost")
        notifier(report_title(cfg, rep),
                 summary_text(rep) + L(" Full report: ", " Ganzer Bericht: ") +
                 f"http://{adr}:{cfg.web_port}/reports/{path.name}", None, "sfd-bericht")
        print(L("Summary queued (the service delivers it).", "Kurzfassung in die Warteschlange gelegt (der Dienst stellt sie zu)."))


def cmd_import_stats(args):
    from .ha import HAClient
    from .history import import_statistics
    cfg = load_config(args.config, args.env)
    clock = Clock(cfg.tz)
    store = Store(cfg.db_path)

    async def go():
        ha = HAClient(cfg.ha_url, cfg.ha_token)
        await ha.ws_connect()
        reader = asyncio.create_task(ha.ws_loop())
        n = await import_statistics(cfg, store, clock, ha)
        reader.cancel()
        await ha.close()
        return n

    print(L(f"{asyncio.run(go())} hourly values imported.", f"{asyncio.run(go())} Stundenwerte übernommen."))


def cmd_scenario(args):
    from .scenarios import SCENARIOS
    cfg, clock, store, det, notifier = _offline(args)
    alias = {"kompressor": "compressor", "grundlast": "baseload", "laufzeit": "runtime", "alle": "all"}
    name = alias.get(args.name, args.name)
    names = list(SCENARIOS) if name == "all" else [name]
    results = []
    for n in names:
        kw = {}
        if args.geraet and n in ("compressor", "runtime"):
            kw["device"] = args.geraet
        r = SCENARIOS[n](cfg, store, notifier if args.senden else None, **kw)
        results.append(r)
        print(f"\n=== {L('Scenario', 'Szenario')}: {r['scenario']} ===")
        print(json.dumps({k: v for k, v in r.items() if k != "messages"}, ensure_ascii=False, indent=1))
        print(L("Notifications:", "Meldungen:"))
        for m in r["messages"]:
            print(f"  {m['title']}: {m['message']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)


def cmd_selftest(args):
    from .ha import HAClient
    cfg = load_config(args.config, args.env)
    ok = True

    async def go():
        nonlocal ok
        ha = HAClient(cfg.ha_url, cfg.ha_token)
        try:
            v = await ha.ws_connect()
            print(L(f"OK   Home Assistant {v} reachable, token valid", f"OK   Home Assistant {v} erreichbar, Token gültig"))
        except Exception as e:  # noqa: BLE001
            print(f"FEHLER Home Assistant: {e}")
            ok = False
            await ha.close()
            return
        ents = [d.leistung for d in cfg.devices.values()] + [d.schalter for d in cfg.devices.values() if d.schalter]
        ents += [getattr(cfg.house, r) for r in ("hausverbrauch", "netzbezug", "einspeisung") if getattr(cfg.house, r)]
        for ent in ents:
            st = await ha.get_state(ent)
            if st is None:
                print(f"WARN {ent}: " + L("not found", "nicht vorhanden"))
            elif st["state"] in ("unavailable", "unknown"):
                print(f"WARN {ent}: {st['state']}")
            else:
                print(f"OK   {ent} = {st['state']} {st['attributes'].get('unit_of_measurement', '')}")
        await ha.close()

    asyncio.run(go())
    sys.exit(0 if ok else 1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sfd", description="Power Hog Detective / Stromfresser-Detektiv")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--env", default=DEFAULT_ENV)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the service").set_defaults(fn=cmd_run)
    sub.add_parser("status", help="learning progress and open findings").set_defaults(fn=cmd_status)
    s = sub.add_parser("ok", help="feedback 'was ok' for a finding id or a device")
    s.add_argument("ziel", metavar="target")
    s.add_argument("--note", "--notiz", dest="notiz")
    s.set_defaults(fn=cmd_ok)
    s = sub.add_parser("findings", aliases=["meldungen"], help="recent findings")
    s.add_argument("--count", "--anzahl", dest="anzahl", type=int, default=30)
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("prelearn", aliases=["vorlernen"], help="learn from Prometheus/HA history")
    s.add_argument("--days", "--tage", dest="tage", type=int)
    s.add_argument("--notify", "--melden", dest="melden", action="store_true", help="send the result as a notification")
    s.set_defaults(fn=cmd_prelearn)
    s = sub.add_parser("report", aliases=["bericht"], help="write a monthly report")
    s.add_argument("--month", "--monat", dest="monat", help="YYYY-MM, default: previous month")
    s.add_argument("--send", "--senden", dest="senden", action="store_true")
    s.set_defaults(fn=cmd_report)
    sub.add_parser("import-statistics", aliases=["statistik-import"],
                   help="import hourly values from HA long-term statistics").set_defaults(fn=cmd_import_stats)
    s = sub.add_parser("scenario", aliases=["szenario"], help="acceptance scenario on a copy of the database")
    s.add_argument("name", choices=["compressor", "baseload", "runtime", "all",
                                    "kompressor", "grundlast", "laufzeit", "alle"])
    s.add_argument("--device", "--geraet", dest="geraet")
    s.add_argument("--send", "--senden", dest="senden", action="store_true", help="really send the [TEST] notifications")
    s.add_argument("--json")
    s.set_defaults(fn=cmd_scenario)
    sub.add_parser("selftest", aliases=["selbsttest"], help="check connection and entities").set_defaults(fn=cmd_selftest)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    args.fn(args)


if __name__ == "__main__":
    main()
