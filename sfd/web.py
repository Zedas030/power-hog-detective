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
from .i18n import L, fmt_datetime, fmt_num, lang

UI_CSS = """
:root{--bg:#f4f5f2;--card:#fff;--fg:#1b2130;--mut:#667085;--line:#e4e6ea;--soft:#eef0f3;
--acc:#2f6fdf;--warn:#c0395a;--warn-bg:#fdf0f3;--ok:#1f8f63;--ok-bg:#eaf7f1;--learn:#a86a12;--learn-bg:#fdf5e6;
--shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1e25;--fg:#e8eaee;--mut:#98a2b3;--line:#2a303a;
--soft:#232933;--acc:#6b9cf0;--warn:#ef7a94;--warn-bg:#3a1f27;--ok:#4cc095;--ok-bg:#16302a;--learn:#e3b25a;
--learn-bg:#352a17;--shadow:none}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 16px 64px}a{color:var(--acc)}.mut{color:var(--mut)}
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px 20px;margin-bottom:22px}
header h1{font-size:26px;line-height:1.2;margin:0;letter-spacing:-.01em}header p{margin:4px 0 0;font-size:13px}
.badge{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;font-weight:600;
font-size:14px;background:var(--soft)}
.badge::before{content:"";width:9px;height:9px;border-radius:50%;background:currentColor}
.badge.unusual,.badge.disconnected{background:var(--warn-bg);color:var(--warn)}
.badge.ok{background:var(--ok-bg);color:var(--ok)}.badge.learning{background:var(--learn-bg);color:var(--learn)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px;background:var(--ok)}
.dot.off{background:var(--warn)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:650;margin:30px 0 10px;
display:flex;align-items:center;gap:8px}
h2 .cnt{background:var(--soft);color:var(--fg);border-radius:999px;padding:0 8px;font-size:12px;letter-spacing:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.kpi span{display:block;color:var(--mut);font-size:13px}.kpi b{display:block;font-size:26px;line-height:1.3;
font-weight:650;font-variant-numeric:tabular-nums}.kpi small{color:var(--mut);font-size:12px}
.kpi.alert b{color:var(--warn)}.kpi.good b{color:var(--ok)}.kpi.learn b{color:var(--learn)}
.finding{display:flex;gap:14px;align-items:center;justify-content:space-between;border-left:4px solid var(--warn);
margin-bottom:10px}.finding p{margin:0}.finding .dev{font-weight:650;display:block;margin-bottom:2px}
.finding form{margin:0}.allgood{border-left:4px solid var(--ok)}
button{font:inherit;font-size:14px;font-weight:600;padding:7px 14px;border-radius:8px;border:1px solid var(--line);
background:var(--soft);color:var(--fg);cursor:pointer;white-space:nowrap;transition:transform .1s}
button:hover{border-color:var(--ok);color:var(--ok)}button:active{transform:scale(.97)}
.devs{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.dev-card{display:flex;flex-direction:column;gap:6px}.dev-card .top{display:flex;justify-content:space-between;
align-items:flex-start;gap:8px}.dev-card .name{font-weight:650;line-height:1.3}
.dev-card .type{display:block;font-size:12px;color:var(--mut);font-weight:400}
.dev-card .w{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}.dev-card .w small{font-size:13px;
color:var(--mut);font-weight:500;margin-left:3px}
.dev-card.bad{border-color:var(--warn)}.dev-card.nodata .w{color:var(--mut)}
.track{height:6px;border-radius:3px;background:var(--soft);overflow:hidden}
.fill{height:100%;border-radius:3px;background:var(--acc)}.bad .fill{background:var(--warn)}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap;
background:var(--soft);color:var(--mut)}
.pill.auf{background:var(--warn-bg);color:var(--warn)}.pill.ok{background:var(--ok-bg);color:var(--ok)}
.pill.learn{background:var(--learn-bg);color:var(--learn)}
.feed{list-style:none;margin:0;padding:0}.feed li{display:grid;grid-template-columns:128px 1fr;gap:12px;padding:9px 0;
border-bottom:1px solid var(--line)}.feed li:last-child{border-bottom:0}.feed time{color:var(--mut);font-size:13px;
font-variant-numeric:tabular-nums;padding-top:1px}
details summary{cursor:pointer;color:var(--acc);font-size:14px;padding-top:10px;list-style:none}
details summary::-webkit-details-marker{display:none}details[open] summary{display:none}
.chips{display:flex;flex-wrap:wrap;gap:8px}.chips a{padding:5px 12px;border-radius:999px;background:var(--card);
border:1px solid var(--line);text-decoration:none;color:var(--fg);font-size:14px;font-variant-numeric:tabular-nums}
.chips a:hover{border-color:var(--acc)}
footer{margin-top:36px;font-size:13px;display:flex;flex-wrap:wrap;gap:6px 16px}
@media (max-width:560px){main{padding-top:20px}header h1{font-size:22px}.finding{flex-direction:column;
align-items:stretch}.finding button{width:100%}.feed li{grid-template-columns:1fr;gap:0}.kpis,.devs{grid-template-columns:1fr 1fr;gap:10px}
.card{padding:12px 13px}.kpi b{font-size:22px}.dev-card .top{flex-direction:column;gap:4px}.dev-card .w{font-size:19px}}
"""

# Reload every minute while the tab is visible (not while older messages are expanded).
_REFRESH_JS = ("<script>setInterval(function(){if(document.visibilityState==='visible'&&"
               "!document.querySelector('details[open]'))location.reload()},60000)</script>")
_FEED_SHOWN = 6


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='{lang()}'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><meta name='color-scheme' content='light dark'>"
            f"<title>{html.escape(title)}</title><style>{UI_CSS}</style></head>"
            f"<body><main>{body}</main>{_REFRESH_JS}</body></html>")


def _split_device(message: str, names: set[str]) -> tuple[str, str]:
    """'TV: standby …' -> ('TV', 'standby …') when the prefix is a known device name."""
    head, sep, rest = message.partition(": ")
    if sep and head in names:
        return head, rest
    return "", message


def build_app(svc) -> web.Application:
    app = web.Application()
    det, store, cfg = svc.det, svc.store, svc.cfg

    async def index(_req):
        st = svc.status()
        e = html.escape
        no_data = L("no data", "keine Daten")
        open_items = [a for a in st["open"] if a["state"] == "offen"]

        # ---- devices: problems first, then by current power ----
        devs = []
        for key, typ in det.targets():
            if key == HOUSE:
                continue
            d = cfg.devices[key]
            lv = det.live[key]
            n, mx = st["learning"][key]
            shown = lv.w is not None and lv.state == "ok" and not lv.entity_missing
            if not shown:
                rank, cls, pill = 1, "nodata", f"<span class='pill auf'>{no_data}</span>"
            elif any(a["dev"] == key for a in open_items):
                rank, cls, pill = 0, "bad", f"<span class='pill auf'>{L('unusual', 'auffällig')}</span>"
            elif n < mx:
                rank, cls, pill = 2, "", f"<span class='pill learn'>{L('learning', 'lernt')} {n}/{mx}</span>"
            else:
                rank, cls, pill = 3, "", "<span class='pill ok'>normal</span>"
            devs.append((rank, -(lv.w or 0) if shown else 0, d.name, TYPE_LABEL[typ], lv.w if shown else None, cls, pill))
        devs.sort(key=lambda x: x[:2])
        peak = max([w for *_, w, _c, _p in devs if w] + [1])
        cards = []
        for _r, _s, name, tlabel, w, cls, pill in devs:
            pct = 0 if not w else max(2, round(w / peak * 100))
            wtxt = f"{fmt_num(w, 1)}<small>W</small>" if w is not None else "–"
            cards.append(f"<div class='card dev-card {cls}'><div class='top'><div class='name'>{e(name)}"
                         f"<span class='type'>{e(tlabel)}</span></div>{pill}</div><div class='w'>{wtxt}</div>"
                         f"<div class='track'><div class='fill' style='width:{pct}%'></div></div></div>")

        # ---- key figures ----
        measured = sum(w for *_, w, _c, _p in devs if w)
        online = sum(1 for *_, w, _c, _p in devs if w is not None)
        learning_n = sum(1 for n, m in st["learning"].values() if n < m)
        kpis = [
            ("alert" if open_items else "good", L("Open findings", "Offene Auffälligkeiten"), str(len(open_items)),
             L("need a look", "brauchen einen Blick") if open_items else L("all quiet", "alles ruhig")),
            ("", L("Measured now", "Gemessen jetzt"), f"{fmt_num(measured, 0)} W",
             L(f"{online} of {len(devs)} plugs reporting", f"{online} von {len(devs)} Steckdosen melden")),
        ]
        if cfg.house.hausverbrauch:
            hl = det.house_live
            hn, hm = st["learning"].get(HOUSE, (0, 0))
            dead = hl.entity_missing or not hl.alive
            sub = (no_data if dead else f"{L('learning', 'lernt')} {hn}/{hm}" if hn < hm
                   else L("unassigned base load watched", "Grundlast wird beobachtet"))
            kpis.append(("alert" if dead else "", L("Whole house", "Haus gesamt"),
                         f"{fmt_num(hl.w, 0)} W" if hl.w and not dead else "–", sub))
        kpis.append(("learn" if learning_n else "good", L("Learning phase", "Lernphase"),
                     str(learning_n) if learning_n else "✓",
                     L(f"{learning_n} still learning", f"{learning_n} lernen noch") if learning_n
                     else L("everything learned", "alles gelernt")))
        kpi_html = "".join(f"<div class='card kpi {c}'><span>{t}</span><b>{v}</b><small>{s}</small></div>"
                           for c, t, v, s in kpis)

        # ---- open findings ----
        names = {d.name for d in cfg.devices.values()}
        ok_btn = L("Was ok", "War ok")
        finds = []
        for a in open_items:
            dev, text = _split_device(a["message"], names)
            head = f"<span class='dev'>{e(dev)}</span>" if dev else ""
            finds.append(f"<div class='card finding'><p>{head}{e(text)}</p>"
                         f"<form method='post' action='/api/feedback'><input type='hidden' name='target' "
                         f"value='{a['id']}'><button title='{L('Mark as normal and learn it', 'Als normal übernehmen und dazulernen')}'>"
                         f"{ok_btn}</button></form></div>")
        open_html = "".join(finds) or (f"<div class='card allgood'>{L('No open findings – nothing to do.', 'Keine offenen Auffälligkeiten – nichts zu tun.')}</div>")

        # ---- notifications ----
        waiting = L("pending", "wartet")
        items = [f"<li><time>{fmt_datetime(svc.clock.local(ts))}</time><span>{e(m)}"
                 f"{'' if sent else f' <span class=pill>{waiting}</span>'}</span></li>"
                 for _i, ts, _t, m, sent in store.sent_messages(30)]
        if not items:
            feed = f"<p class='mut' style='margin:0'>{L('None yet.', 'Noch keine.')}</p>"
        else:
            feed = f"<ul class='feed'>{''.join(items[:_FEED_SHOWN])}</ul>"
            if len(items) > _FEED_SHOWN:
                more = L(f"Show {len(items) - _FEED_SHOWN} older", f"{len(items) - _FEED_SHOWN} ältere anzeigen")
                feed += f"<details><summary>{more}</summary><ul class='feed'>{''.join(items[_FEED_SHOWN:])}</ul></details>"

        # ---- reports ----
        rdir = Path(cfg.report_dir)
        reports = sorted(list(rdir.glob("report-*.html")) + list(rdir.glob("bericht-*.html")),
                         key=lambda p: p.stem.split("-", 1)[1], reverse=True) if rdir.exists() else []
        rep_html = ("<div class='chips'>" + "".join(
            f"<a href='/reports/{p.name}'>{p.stem.split('-', 1)[1]}</a>" for p in reports) + "</div>") if reports else \
            f"<p class='mut' style='margin:0'>{L('None yet (written on the 1st of the month).', 'Noch keiner (erscheint am Monatsersten).')}</p>"

        ha_ok = det.ha_connected
        ha_txt = L("connected", "verbunden") if ha_ok else L("not connected", "nicht verbunden")
        cnt = f"<span class='cnt'>{len(open_items)}</span>" if open_items else ""
        body = (f"<header><div><h1>{e(cfg.app_name)}</h1><p class='mut'><span class='dot{'' if ha_ok else ' off'}'>"
                f"</span>Home Assistant {ha_txt} · Version {__version__}</p></div>"
                f"<span class='badge {e(st['state'])}'>{e(state_text(st['state']))}</span></header>"
                f"<section class='kpis'>{kpi_html}</section>"
                f"<h2>{L('Open findings', 'Offene Auffälligkeiten')}{cnt}</h2>{open_html}"
                f"<h2>{L('Devices', 'Geräte')}<span class='cnt'>{len(devs)}</span></h2><div class='devs'>{''.join(cards)}</div>"
                f"<h2>{L('Recent notifications', 'Letzte Meldungen')}</h2><div class='card'>{feed}</div>"
                f"<h2>{L('Monthly reports', 'Monatsberichte')}</h2>{rep_html}"
                f"<footer class='mut'><a href='/metrics'>{L('Prometheus metrics', 'Prometheus-Metriken')}</a>"
                f"<a href='/api/status'>Status (JSON)</a><span>{L('Refreshes every minute', 'Aktualisiert sich jede Minute')}</span></footer>")
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
