"""Small web UI (status, findings, reports), /metrics for Prometheus and /health."""
from __future__ import annotations

import html
import json
from pathlib import Path

from aiohttp import web

from . import __version__
from .config import TYPE_LABEL
from .app import state_text
from .detector import HOUSE
from .metrics import render
from .report import CSS
from .i18n import L, fmt_datetime, fmt_num


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{CSS}"
            f".pill{{display:inline-block;padding:1px 9px;border-radius:9px;font-size:12px;border:1px solid var(--line)}}"
            f".pill.auf{{color:var(--warn);border-color:var(--warn)}}.pill.ok{{color:var(--ok);border-color:var(--ok)}}"
            f"button{{font:inherit;padding:4px 10px;border-radius:7px;border:1px solid var(--line);"
            f"background:var(--card);color:var(--fg);cursor:pointer}}</style></head><body><main>{body}</main></body></html>")


def build_app(svc) -> web.Application:
    app = web.Application()
    det, store, cfg = svc.det, svc.store, svc.cfg

    async def index(_req):
        st = svc.status()
        e = html.escape
        rows = []
        no_data = L("no data", "keine Daten")
        for key, typ in det.targets():
            if key == HOUSE:
                continue
            d = cfg.devices[key]
            lv = det.live[key]
            n, mx = st["learning"][key]
            mine = [a for a in st["open"] if a["dev"] == key and a["state"] == "offen"]
            if lv.entity_missing or lv.state != "ok":
                pill = f"<span class='pill auf'>{no_data}</span>"
            elif mine:
                pill = f"<span class='pill auf'>{L('unusual', 'auffällig')}</span>"
            elif n < mx:
                pill = f"<span class='pill'>{L('learning', 'lernt')} {n}/{mx}</span>"
            else:
                pill = "<span class='pill ok'>normal</span>"
            shown = lv.w is not None and lv.state == "ok" and not lv.entity_missing
            rows.append(f"<tr><td>{e(d.name)}<br><span class='mut' style='font-size:12px'>{e(TYPE_LABEL[typ])}"
                        f"</span></td><td>{fmt_num(lv.w, 1) + ' W' if shown else '–'}</td><td>{pill}</td></tr>")
        hn, hm = st["learning"].get(HOUSE, (0, 0))
        if cfg.house.hausverbrauch:
            hl = det.house_live
            pill = (f"<span class='pill auf'>{no_data}</span>" if (hl.entity_missing or not hl.alive)
                    else f"<span class='pill'>{L('learning', 'lernt')} {hn}/{hm}</span>" if hn < hm
                    else "<span class='pill ok'>normal</span>")
            rows.append(f"<tr><td>{L('Whole house', 'Haus gesamt')}<br><span class='mut' style='font-size:12px'>"
                        f"{L('base load, unassigned', 'Grundlast, nicht zugeordnet')}</span></td>"
                        f"<td>{fmt_num(hl.w, 0) + ' W' if hl.w else '–'}</td><td>{pill}</td></tr>")
        ok_btn = L("Was ok", "War ok")
        open_html = "".join(
            f"<li>{e(a['message'])} <form method='post' action='/api/feedback' style='display:inline'>"
            f"<input type='hidden' name='target' value='{a['id']}'><button>{ok_btn}</button></form></li>"
            for a in st["open"] if a["state"] == "offen") or \
            f"<li class='mut'>{L('No open findings.', 'Keine offenen Auffälligkeiten.')}</li>"
        waiting = L("pending", "wartet")
        msgs = "".join(f"<li><span class='mut'>{fmt_datetime(svc.clock.local(ts))}</span> "
                       f"{e(m)}{'' if sent else f' <span class=pill>{waiting}</span>'}</li>"
                       for _i, ts, _t, m, sent in store.sent_messages(15)) or \
            f"<li class='mut'>{L('None yet.', 'Noch keine.')}</li>"
        rdir = Path(cfg.report_dir)
        reports = sorted(list(rdir.glob("report-*.html")) + list(rdir.glob("bericht-*.html")),
                         key=lambda p: p.stem.split("-", 1)[1], reverse=True) if rdir.exists() else []
        rep_html = "".join(f"<li><a href='/reports/{p.name}'>{p.stem.split('-', 1)[1]}</a></li>" for p in reports) or \
            f"<li class='mut'>{L('None yet (written on the 1st of the month).', 'Noch keiner (erscheint am Monatsersten).')}</li>"
        ha_txt = L("connected", "verbunden") if det.ha_connected else L("not connected", "nicht verbunden")
        body = (f"<h1>{e(cfg.app_name)}</h1><p class='mut'>Status: <b>{e(state_text(st['state']))}</b> · Version "
                f"{__version__} · Home Assistant {ha_txt}</p>"
                f"<h2>{L('Open findings', 'Offene Auffälligkeiten')}</h2><div class='card'><ul>{open_html}</ul></div>"
                f"<h2>{L('Devices', 'Geräte')}</h2><div class='card tbl'><table><thead><tr><th>{L('Device', 'Gerät')}"
                f"</th><th>{L('Power', 'Leistung')}</th><th>{L('State', 'Zustand')}</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></div>"
                f"<h2>{L('Recent notifications', 'Letzte Meldungen')}</h2><div class='card'><ul>{msgs}</ul></div>"
                f"<h2>{L('Monthly reports', 'Monatsberichte')}</h2><div class='card'><ul>{rep_html}</ul></div>"
                f"<p class='mut'><a href='/metrics'>{L('Prometheus metrics', 'Prometheus-Metriken')}</a> · "
                f"<a href='/api/status'>Status (JSON)</a></p>")
        return web.Response(text=_page(cfg.app_name, body), content_type="text/html")

    async def report(req):
        name = req.match_info["name"]
        if "/" in name or "\\" in name or not (name.startswith("bericht-") or name.startswith("report-")):
            raise web.HTTPNotFound()
        p = Path(cfg.report_dir) / name
        if not p.exists():
            raise web.HTTPNotFound()
        return web.Response(text=p.read_text(encoding="utf-8"), content_type="text/html")

    async def metrics(_req):
        return web.Response(text=render(det, {"errors_total": svc.errors}), content_type="text/plain")

    async def health(_req):
        ok = True
        return web.json_response({"ok": ok, "ha_connected": det.ha_connected, "version": __version__,
                                  "uptime_s": round(svc.clock.now() - svc.started)})

    async def status(_req):
        st = svc.status()
        learn = {k: f"{n}/{m}" for k, (n, m) in st["learning"].items()}
        items = [{"id": a["id"], "device": a["dev"], "message": a["message"], "state": a["state"]} for a in st["open"]]
        out = {"status": st["state"], "learning": learn, "open": items}
        if cfg.language == "de":  # German keys kept for existing consumers
            out.update({"lernfortschritt": learn,
                        "offen": [{"id": i["id"], "geraet": i["device"], "meldung": i["message"],
                                   "zustand": i["state"]} for i in items]})
        return web.Response(text=json.dumps(out, ensure_ascii=False, indent=1), content_type="application/json")

    async def feedback(req):
        if req.content_type == "application/json":
            data = await req.json()
        else:
            data = await req.post()
        target = data.get("target") or data.get("ziel")
        try:
            done = det.feedback(target, source="web")
        except KeyError as ex:
            raise web.HTTPBadRequest(text=str(ex))
        await svc.publish()
        if req.content_type == "application/json":
            return web.json_response({"accepted": [a["id"] for a in done]})
        raise web.HTTPSeeOther("/")

    app.router.add_get("/", index)
    app.router.add_get("/reports/{name}", report)
    app.router.add_get("/berichte/{name}", report)
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/health", health)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/feedback", feedback)
    return app


async def start_web(svc):
    runner = web.AppRunner(build_app(svc), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", svc.cfg.web_port)
    await site.start()
    return runner
