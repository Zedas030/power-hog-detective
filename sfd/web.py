"""Small web UI (status, findings, reports), /metrics for Prometheus and /health."""
from __future__ import annotations

import html
import json
from pathlib import Path

from aiohttp import web

from . import __version__
from .config import TYPE_LABEL
from .detector import HOUSE
from .metrics import render
from .report import CSS
from .util import fmt_num


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
        for key, typ in det.targets():
            if key == HOUSE:
                continue
            d = cfg.devices[key]
            lv = det.live[key]
            n, mx = st["learning"][key]
            mine = [a for a in st["open"] if a["dev"] == key and a["state"] == "offen"]
            if lv.entity_missing or lv.state != "ok":
                pill = "<span class='pill auf'>keine Daten</span>"
            elif mine:
                pill = "<span class='pill auf'>auffällig</span>"
            elif n < mx:
                pill = f"<span class='pill'>lernt {n}/{mx}</span>"
            else:
                pill = "<span class='pill ok'>normal</span>"
            rows.append(f"<tr><td>{e(d.name)}<br><span class='mut' style='font-size:12px'>{e(TYPE_LABEL[typ])}"
                        f"</span></td><td>{fmt_num(lv.w, 1) + ' W' if (lv.w is not None and lv.state == 'ok' and not lv.entity_missing) else '–'}"
                        f"</td><td>{pill}</td></tr>")
        hn, hm = st["learning"].get(HOUSE, (0, 0))
        if cfg.house.hausverbrauch:
            hl = det.house_live
            pill = ("<span class='pill auf'>keine Daten</span>" if (hl.entity_missing or not hl.alive)
                    else f"<span class='pill'>lernt {hn}/{hm}</span>" if hn < hm else "<span class='pill ok'>normal</span>")
            rows.append(f"<tr><td>Haus gesamt<br><span class='mut' style='font-size:12px'>Grundlast, nicht "
                        f"zugeordnet</span></td><td>{fmt_num(hl.w, 0) + ' W' if hl.w else '–'}</td><td>{pill}</td></tr>")
        open_html = "".join(
            f"<li>{e(a['message'])} <form method='post' action='/api/feedback' style='display:inline'>"
            f"<input type='hidden' name='ziel' value='{a['id']}'><button>War ok</button></form></li>"
            for a in st["open"] if a["state"] == "offen") or "<li class='mut'>Keine offenen Auffälligkeiten.</li>"
        msgs = "".join(f"<li><span class='mut'>{svc.clock.local(ts).strftime('%d.%m. %H:%M')}</span> "
                       f"{e(m)}{'' if sent else ' <span class=pill>wartet</span>'}</li>"
                       for _i, ts, _t, m, sent in store.sent_messages(15)) or "<li class='mut'>Noch keine.</li>"
        reports = sorted(Path(cfg.report_dir).glob("bericht-*.html"), reverse=True) if Path(cfg.report_dir).exists() else []
        rep_html = "".join(f"<li><a href='/berichte/{p.name}'>{p.stem.replace('bericht-', '')}</a></li>"
                           for p in reports) or "<li class='mut'>Noch keiner (erscheint am Monatsersten).</li>"
        body = (f"<h1>Stromfresser-Detektiv</h1><p class='mut'>Status: <b>{e(st['state'])}</b> · Version "
                f"{__version__} · Home Assistant {'verbunden' if det.ha_connected else 'nicht verbunden'}</p>"
                f"<h2>Offene Auffälligkeiten</h2><div class='card'><ul>{open_html}</ul></div>"
                f"<h2>Geräte</h2><div class='card tbl'><table><thead><tr><th>Gerät</th><th>Leistung</th><th>Zustand"
                f"</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
                f"<h2>Letzte Meldungen</h2><div class='card'><ul>{msgs}</ul></div>"
                f"<h2>Monatsberichte</h2><div class='card'><ul>{rep_html}</ul></div>"
                f"<p class='mut'><a href='/metrics'>Prometheus-Metriken</a> · <a href='/api/status'>Status (JSON)</a></p>")
        return web.Response(text=_page("Stromfresser-Detektiv", body), content_type="text/html")

    async def report(req):
        name = req.match_info["name"]
        if "/" in name or "\\" in name or not name.startswith("bericht-"):
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
        return web.Response(text=json.dumps({
            "status": st["state"],
            "lernfortschritt": {k: f"{n}/{m}" for k, (n, m) in st["learning"].items()},
            "offen": [{"id": a["id"], "geraet": a["dev"], "meldung": a["message"], "zustand": a["state"]}
                      for a in st["open"]],
        }, ensure_ascii=False, indent=1), content_type="application/json")

    async def feedback(req):
        if req.content_type == "application/json":
            data = await req.json()
        else:
            data = await req.post()
        target = data.get("ziel")
        try:
            done = det.feedback(target, source="web")
        except KeyError as ex:
            raise web.HTTPBadRequest(text=str(ex))
        await svc.publish()
        if req.content_type == "application/json":
            return web.json_response({"bestaetigt": [a["id"] for a in done]})
        raise web.HTTPSeeOther("/")

    app.router.add_get("/", index)
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
