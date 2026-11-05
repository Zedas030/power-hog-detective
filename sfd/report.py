"""Monthly report: consumption and cost per device, solar share, base load trend, top-5 savings."""
from __future__ import annotations

import calendar
import html
from datetime import date, timedelta
from pathlib import Path

from .config import TYPE_LABEL, Config
from .detector import HOUSE, Detector
from .store import Store
from .i18n import L, fmt_date, fmt_datetime, fmt_num, lang
from .i18n import month as month_name
from .util import Clock, median

PALETTE = ["#2f6fdf", "#e0702a", "#2a9d6f", "#c0395a", "#7b5cd6", "#c79a12", "#1f9bb4", "#8a6d55",
           "#d65fb0", "#5f7d2a", "#4a5568", "#b04a2a", "#3c8dbc"]


def plugs_kwh_total(rows):
    return sum(r["kwh"] for r in rows)


def month_bounds(clock: Clock, year: int, month: int):
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last, clock.day_bounds(first)[0], clock.day_bounds(last)[1]


def _hourly(store: Store, dev: str, s: float, e: float) -> dict:
    return {int(t): wh for t, wh in store.energy(dev, s, e)}


def build(cfg: Config, store: Store, det: Detector, clock: Clock, year: int, month: int) -> dict:
    first, last, s, e = month_bounds(clock, year, month)
    n_days = (last - first).days + 1
    price = cfg.price_import
    has_house = bool(cfg.house.hausverbrauch)
    house = _hourly(store, f"{HOUSE}:hausverbrauch", s, e) if has_house else {}
    grid = _hourly(store, f"{HOUSE}:netzbezug", s, e) if (has_house and cfg.house.netzbezug) else {}
    export = _hourly(store, f"{HOUSE}:einspeisung", s, e) if (has_house and cfg.house.einspeisung) else {}
    plug_sum_h = {}
    for key in cfg.devices:
        for h, wh in _hourly(store, key, s, e).items():
            plug_sum_h[h] = plug_sum_h.get(h, 0) + wh
    # drop implausible house hours (house below the metered plugs, e.g. a sensor stuck at 0)
    bad = {h for h, wh in house.items() if wh < 0.9 * plug_sum_h.get(h, 0) - 5}
    for h in bad:
        house.pop(h, None)
        grid.pop(h, None)
        export.pop(h, None)

    solar = {}
    for ent in cfg.house.solar:
        for h, wh in _hourly(store, f"_solar:{ent}", s, e).items():
            solar[h] = solar.get(h, 0) + max(wh, 0)
    solar_estimated = not grid and bool(solar)

    def grid_frac(h):
        if h in house and h in grid and house[h] > 1:
            return min(1.0, max(0.0, grid[h] / house[h]))
        if solar_estimated and plug_sum_h.get(h, 0) > 1:
            # no meter: solar/battery output into the house covers the plugs first (zero-export control)
            return 1.0 - min(1.0, solar.get(h, 0) / plug_sum_h[h])
        return None

    have_house_hours = sum(1 for h in house if h in grid)
    dev_rows = []
    plug_hourly = {}
    for key, d in cfg.devices.items():
        hourly = _hourly(store, key, s, e)
        plug_hourly[key] = hourly
        kwh = sum(hourly.values()) / 1000
        cost = 0.0
        grid_kwh = 0.0
        for h, wh in hourly.items():
            f = grid_frac(h)
            f = 1.0 if f is None else f
            grid_kwh += wh * f / 1000
        cost = grid_kwh * price
        hours_with_data = len(hourly)
        dev_rows.append({
            "key": key, "name": d.name, "typ": TYPE_LABEL[d.typ], "kwh": kwh, "netz_kwh": grid_kwh,
            "kosten": cost, "abdeckung": hours_with_data / (n_days * 24),
            "solar_anteil": (1 - grid_kwh / kwh) if kwh > 0 else None,
        })
    dev_rows.sort(key=lambda r: -r["kwh"])

    house_kwh = sum(house.values()) / 1000 if house else None
    grid_kwh = sum(grid.values()) / 1000 if grid else None
    export_kwh = sum(export.values()) / 1000 if export else None
    plugs_kwh = sum(r["kwh"] for r in dev_rows)
    solar_share = (1 - grid_kwh / house_kwh) if (house_kwh and grid_kwh is not None) else None
    solar_kwh = sum(solar.values()) / 1000 if solar else None
    if solar_share is None and solar_estimated and plugs_kwh_total(dev_rows) > 0:
        solar_share = 1 - sum(r["netz_kwh"] for r in dev_rows) / plugs_kwh_total(dev_rows)

    # base load per day from hourly values in the night window (works for every month with statistics)
    base_days = []
    h0, h1 = cfg.house.nacht_von // 60, max(cfg.house.nacht_bis // 60, cfg.house.nacht_von // 60 + 1)
    for i in range(n_days):
        d = first + timedelta(days=i)
        hs = [int(clock.at(d, hh)) for hh in range(h0, h1)]
        hv = [house[h] for h in hs if h in house]
        pv = [sum(plug_hourly[k].get(h, 0) for k in plug_hourly) for h in hs]
        if house and len(hv) == len(hs):
            nb = sum(hv) / len(hv)
            base_days.append((d, nb, nb - sum(pv) / len(pv)))
        elif not house and all(any(h in plug_hourly[k] for k in plug_hourly) for h in hs):
            base_days.append((d, sum(pv) / len(pv), None))  # no meter: base load of the metered plugs only
        else:
            base_days.append((d, None, None))
    nb_vals = [b for _, b, _ in base_days if b is not None]
    un_vals = [u for _, _, u in base_days if u is not None]

    # unassigned high-load hours (likely the instantaneous water heater)
    un_base = median(un_vals) or 0.0
    dle_kwh = 0.0
    dle_hours = 0
    for h, wh in house.items():
        un = wh - sum(plug_hourly[k].get(h, 0) for k in plug_hourly) - un_base
        if un > 1000:
            dle_kwh += un / 1000
            dle_hours += 1

    savings = _savings(cfg, store, clock, det, dev_rows, plug_hourly, house, grid, first, last, n_days,
                       un_vals, dle_kwh, grid_frac)
    anomalies = [a for a in store.recent_anomalies(1000)
                 if a["test"] == 0 and a["state"] != "fehlalarm" and a["start_day"]
                 and first.isoformat() <= a["start_day"] <= last.isoformat()]
    anomalies.sort(key=lambda a: a["start_day"])
    return {
        "jahr": year, "monat": month, "titel": f"{month_name(month - 1)} {year}", "tage": n_days,
        "preis": price, "einspeiseverguetung": cfg.price_export,
        "geraete": dev_rows, "haus_kwh": house_kwh, "netz_kwh": grid_kwh, "einspeisung_kwh": export_kwh,
        "steckdosen_kwh": plugs_kwh, "solar_anteil": solar_share, "solar_kwh": solar_kwh,
        "solar_geschaetzt": solar_estimated,
        "hausdaten_stunden": have_house_hours, "stunden": n_days * 24,
        "grundlast": base_days, "grundlast_median": median(nb_vals), "unzugeordnet_median": median(un_vals),
        "dle_kwh": dle_kwh, "dle_stunden": dle_hours, "sparpotenziale": savings, "auffaelligkeiten": anomalies,
        "netzkosten": (grid_kwh * price) if grid_kwh is not None else sum(r["kosten"] for r in dev_rows),
        "einspeise_erloes": (export_kwh or 0) * cfg.price_export,
        "hinweise": list((cfg.raw.get("bericht") or {}).get("hinweise") or []),
    }


def eur(x, digits=0) -> str:
    return L(f"€{fmt_num(x, digits)}", f"{fmt_num(x, digits)} €")


def _savings(cfg, store, clock, det, rows, plug_hourly, house, grid, first, last, n_days, un_vals, dle_kwh,
             grid_frac):
    price = cfg.price_import
    year_factor = 365 / n_days
    out = []
    by_key = {r["key"]: r for r in rows}
    noon_fracs = []
    night_fracs = []
    hours = house or {h for k in plug_hourly for h in plug_hourly[k]}
    for h in hours:
        f = grid_frac(h)
        if f is None:
            continue
        hour = clock.local(h).hour
        if 11 <= hour < 15:
            noon_fracs.append(f)
        if cfg.house.nacht_von // 60 <= hour < cfg.house.nacht_bis // 60:
            night_fracs.append(f)
    noon = sum(noon_fracs) / len(noon_fracs) if noon_fracs else None
    night = sum(night_fracs) / len(night_fracs) if night_fracs else 1.0
    n0, n1 = cfg.house.nacht_von // 60, cfg.house.nacht_bis // 60

    for key, d in cfg.devices.items():
        r = by_key[key]
        if r["kwh"] <= 0:
            continue
        stats = [store.daily_day(key, (first + timedelta(days=i)).isoformat()) for i in range(n_days)]
        standby = median([x.get("standby_w") for x in stats if x.get("standby_w") is not None])
        on_h = median([x.get("an_stunden") for x in stats if x.get("an_stunden") is not None]) or 0
        # 1) standby that could be switched off
        if d.typ in ("nutzung", "programm", "ladegeraet") and standby and standby >= 1.0:
            kwh_y = standby * (24 - on_h) * 365 / 1000
            out.append({"eur": kwh_y * price * night, "titel": L(f"{d.name}: switch off standby",
                                                                 f"{d.name}: Standby abschalten"),
                        "text": L(f"{fmt_num(standby, 1)} W standby for about {fmt_num(24 - on_h, 0)} h a day "
                                  f"≈ {fmt_num(kwh_y, 0)} kWh/year. Switch the plug off at night/when unused by "
                                  f"schedule or use a power strip with a switch.",
                                  f"{fmt_num(standby, 1)} W Standby an rund {fmt_num(24 - on_h, 0)} Std. pro Tag "
                                  f"≈ {fmt_num(kwh_y, 0)} kWh/Jahr. Steckdose nachts/bei Nichtnutzung per Zeitplan "
                                  f"ausschalten oder Steckerleiste mit Schalter.")})
        # 1b) usage devices that keep running at night (measured in the night window)
        if d.typ == "nutzung":
            night_vals = [wh for h, wh in plug_hourly[key].items() if n0 <= clock.local(h).hour < n1]
            if len(night_vals) >= 24:
                night_w = sum(night_vals) / len(night_vals)
                if night_w >= max(20.0, (standby or 0) * 2):
                    hrs = (cfg.house.nacht_bis - cfg.house.nacht_von) / 60
                    kwh_y = (night_w - (standby or 0)) * hrs * 365 / 1000
                    out.append({"eur": kwh_y * price * night,
                                "titel": L(f"{d.name}: runs at night", f"{d.name}: läuft nachts"),
                                "text": L(f"Between {n0:02d}:00 and {n1:02d}:00 on average {fmt_num(night_w, 0)} W "
                                          f"(≈ {fmt_num(kwh_y, 0)} kWh/year in these {fmt_num(hrs, 0)} hours alone). "
                                          f"Use sleep/standby after inactivity or shut down at night.",
                                          f"Zwischen {n0:02d} und {n1:02d} Uhr im Schnitt {fmt_num(night_w, 0)} W "
                                          f"(≈ {fmt_num(kwh_y, 0)} kWh/Jahr allein in diesen {fmt_num(hrs, 0)} "
                                          f"Stunden). Energiesparmodus/Ruhezustand nach Inaktivität oder nachts "
                                          f"herunterfahren.")})
        # 2) fridge vs. efficient reference
        if d.typ == "zyklus" and det.profile(key) == "kuehlgeraet":
            kwh_y = r["kwh"] * year_factor
            ref = cfg.fridge_reference_kwh
            if kwh_y > ref:
                out.append({"eur": (kwh_y - ref) * price * (r["netz_kwh"] / r["kwh"]),
                            "titel": L(f"{d.name}: above reference", f"{d.name}: Verbrauch über Referenz"),
                            "text": L(f"Extrapolated {fmt_num(kwh_y, 0)} kWh/year, an efficient appliance needs about "
                                      f"{fmt_num(ref, 0)} kWh. Check first: 7 °C setting, door seal, free rear vent, "
                                      f"defrost; otherwise consider a replacement.",
                                      f"Hochgerechnet {fmt_num(kwh_y, 0)} kWh/Jahr, ein effizientes Gerät braucht "
                                      f"etwa {fmt_num(ref, 0)} kWh. Erst prüfen: Temperatur auf 7 °C, Dichtung, "
                                      f"Rückwand frei, abtauen; sonst Ersatz erwägen.")})
        # 3) shift flexible loads into solar hours
        if d.typ in ("programm", "zeitplan") or det.profile(key) == "boiler":
            if noon is not None and r["kwh"] > 0:
                gain = (r["netz_kwh"] * price - r["kwh"] * noon * price) * year_factor
                if gain > 1:
                    now_pct = fmt_num(100 * r["netz_kwh"] / r["kwh"], 0)
                    out.append({"eur": gain, "titel": L(f"{d.name}: move into sunny hours",
                                                        f"{d.name}: in Sonnenstunden verlegen"),
                                "text": L(f"So far {now_pct} % from the grid; between 11:00 and 15:00 it would be only "
                                          f"{fmt_num(100 * noon, 0)} % on average. Move the start time/schedule to "
                                          f"midday.",
                                          f"Bisher {now_pct} % aus dem Netz; zwischen 11 und 15 Uhr wären es im Schnitt "
                                          f"nur {fmt_num(100 * noon, 0)} %. Startzeit/Zeitplan auf die Mittagszeit "
                                          f"legen.")})
    # 4) unassigned base load
    un = median(un_vals)
    if un and un > 10:
        share = float((cfg.raw.get("bericht") or {}).get("grundlast_sparanteil", 0.3))
        kwh_y = un * 8.76
        out.append({"eur": kwh_y * price * night * share,
                    "titel": L("Track down unassigned base load", "Nicht zugeordnete Grundlast aufspüren"),
                    "text": L(f"At night {fmt_num(un, 0)} W run on average that no metering plug sees "
                              f"(≈ {fmt_num(kwh_y, 0)} kWh/year). Assumed {fmt_num(share * 100, 0)} % of it avoidable "
                              f"(power supplies, standby without metering plug, circulation pump). Tip: switch off "
                              f"single circuits briefly at night and watch the house consumption.",
                              f"Nachts laufen im Mittel {fmt_num(un, 0)} W, die keine Messsteckdose sieht "
                              f"(≈ {fmt_num(kwh_y, 0)} kWh/Jahr). Gerechnet mit {fmt_num(share * 100, 0)} % davon "
                              f"vermeidbar (Netzteile, Standby ohne Messsteckdose, Umwälzpumpe). Tipp: nachts "
                              f"einzelne Sicherungen kurz ausschalten und auf den Hausverbrauch schauen.")})
    # 5) instantaneous water heater
    if dle_kwh > 1 and cfg.house.dle_aktiv:
        kwh_y = dle_kwh * year_factor
        nm = cfg.house.dle_name
        pct = fmt_num(cfg.dle_saving_share * 100, 0)
        out.append({"eur": kwh_y * price * cfg.dle_saving_share,
                    "titel": L(f"{nm}: water-saving shower head", f"{nm}: Sparduschkopf"),
                    "text": L(f"Roughly {fmt_num(dle_kwh, 0)} kWh this month from large load peaks that no plug sees "
                              f"(probably the {nm}). A water-saving shower head typically saves {pct} %; also limit "
                              f"the temperature at the device to 38–40 °C.",
                              f"Grob geschätzt {fmt_num(dle_kwh, 0)} kWh im Monat aus großen, keiner Steckdose "
                              f"zuzuordnenden Lastspitzen (vermutlich {nm}). Ein Sparduschkopf spart typisch {pct} %, "
                              f"zusätzlich Temperatur am Gerät auf 38–40 °C begrenzen.")})
    out.sort(key=lambda x: -x["eur"])
    return out[:5]


# ---------------------------------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d2330;--mut:#5b6474;--line:#e3e5e9;--acc:#2f6fdf;--warn:#c0395a;--ok:#2a9d6f}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e8eaee;--mut:#9aa3b2;--line:#2c323c;--acc:#6b9cf0;--warn:#e06b86;--ok:#4cc095}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 12px}
.mut{color:var(--mut)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.kpi span{color:var(--mut);font-size:13px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}th{font-size:13px;color:var(--mut);font-weight:600}
.tbl{overflow-x:auto}.bar{height:10px;border-radius:5px;background:var(--acc)}
ol.save li{margin:0 0 12px}ol.save b.eur{color:var(--ok)}.warn{color:var(--warn)}svg text{fill:var(--mut);font-size:11px}
"""


def _svg_baseload(days, chart_label, night_label):
    pts = [(d, b, u) for d, b, u in days if b is not None]
    if not pts:
        return f"<p class='mut'>{L('No base load data this month.', 'Keine Daten für die Grundlast in diesem Monat.')}</p>"
    w, h, pad = 900, 220, 36
    mx = max(max(b for _, b, _ in pts), 1) * 1.15
    n = len(days)
    xs = lambda i: pad + i * (w - 2 * pad) / max(n - 1, 1)  # noqa: E731
    ys = lambda v: h - pad - (max(v, 0) / mx) * (h - 2 * pad)  # noqa: E731
    idx = {d: i for i, (d, _, _) in enumerate(days)}
    line_b = " ".join(f"{xs(idx[d]):.1f},{ys(b):.1f}" for d, b, _ in pts)
    line_u = " ".join(f"{xs(idx[d]):.1f},{ys(u):.1f}" for d, _, u in pts if u is not None)
    grid = "".join(f"<line x1='{pad}' x2='{w - pad}' y1='{ys(v):.1f}' y2='{ys(v):.1f}' stroke='var(--line)'/>"
                   f"<text x='4' y='{ys(v) + 4:.1f}'>{fmt_num(v)} W</text>" for v in (0, mx / 2, mx / 1.15))
    labels = "".join(f"<text x='{xs(i):.1f}' y='{h - 10}' text-anchor='middle'>{d.day}.</text>"
                     for i, (d, _, _) in enumerate(days) if d.day in (1, 8, 15, 22, 29))
    aria = L("Night base load", "Nächtliche Grundlast")
    return (f"<svg viewBox='0 0 {w} {h}' width='100%' role='img' aria-label='{aria}'>{grid}{labels}"
            f"<polyline fill='none' stroke='var(--acc)' stroke-width='2.5' points='{line_b}'/>"
            f"<polyline fill='none' stroke='#e0702a' stroke-width='2' stroke-dasharray='5 4' points='{line_u}'/></svg>"
            f"<p class='mut'><span style='color:var(--acc)'>━</span> {chart_label} ({L('mean', 'Mittel')} {night_label})"
            + (" &nbsp; <span style='color:#e0702a'>╍</span> " + L("of which not attributable to any plug",
                                                                    "davon keiner Steckdose zuzuordnen")
               if line_u else "") + "</p>")


def report_title(cfg: Config, rep: dict) -> str:
    return L(f"{cfg.short_name} report {rep['titel']}", f"{cfg.short_name}-Bericht {rep['titel']}")


def render_html(rep: dict, cfg: Config, generated: str) -> str:
    house_known = rep["haus_kwh"] is not None
    chart_label = (L("House base load", "Grundlast Haus") if house_known
                   else L("Base load of the metering plugs", "Grundlast der Messsteckdosen"))
    night_label = L(f"{cfg.house.nacht_von // 60:02d}:00–{cfg.house.nacht_bis // 60:02d}:00",
                    f"{cfg.house.nacht_von // 60:02d}–{cfg.house.nacht_bis // 60:02d} Uhr")
    e = html.escape
    rows = rep["geraete"]
    mx = max([r["kwh"] for r in rows] + [0.001])
    trs = "".join(
        f"<tr><td>{e(r['name'])}<br><span class='mut' style='font-size:12px'>{e(r['typ'])}</span></td>"
        f"<td style='min-width:120px'><div class='bar' style='width:{100 * r['kwh'] / mx:.0f}%;"
        f"background:{PALETTE[i % len(PALETTE)]}'></div></td>"
        f"<td>{fmt_num(r['kwh'], 1)}</td><td>{eur(r['kosten'], 2)}</td>"
        f"<td>{'–' if r['solar_anteil'] is None else fmt_num(100 * r['solar_anteil'], 0) + ' %'}</td>"
        f"<td>{fmt_num(100 * r['abdeckung'], 0)} %</td></tr>"
        for i, r in enumerate(rows))
    no_data = L("no data", "keine Daten")
    kpis = []
    if house_known:
        kpis.append((L("House consumption", "Hausverbrauch"), f"{fmt_num(rep['haus_kwh'], 0)} kWh"))
    kpis.append((L("of which metering plugs", "davon Messsteckdosen"), f"{fmt_num(rep['steckdosen_kwh'], 0)} kWh"))
    if house_known:
        kpis.append((L("Grid import", "Netzbezug"),
                     f"{fmt_num(rep['netz_kwh'], 0)} kWh" if rep["netz_kwh"] is not None else no_data))
    kpis += [
        (L("Solar/battery share", "Anteil Solar/Batterie") + (L(" (estimated)", " (geschätzt)")
                                                              if rep["solar_geschaetzt"] else ""),
         f"{fmt_num(100 * rep['solar_anteil'], 0)} %" if rep["solar_anteil"] is not None else no_data),
        (L("Solar/battery into the house", "Balkonkraftwerk ins Haus"),
         f"{fmt_num(rep['solar_kwh'], 0)} kWh" if rep["solar_kwh"] is not None else no_data),
        (L("Grid electricity cost", "Stromkosten Netz") + ("" if house_known else L(" (metering plugs)",
                                                                                    " (Messsteckdosen)")),
         eur(rep["netzkosten"], 2)),
        (L("Night base load", "Nächtliche Grundlast"),
         f"{fmt_num(rep['grundlast_median'], 0)} W" if rep["grundlast_median"] else "–"),
    ]
    kpi_html = "".join(f"<div class='card kpi'><span>{k}</span><b>{v}</b></div>" for k, v in kpis)
    per_year = L("/year", "/Jahr")
    saves = "".join(f"<li><b>{e(s['titel'])}</b> – <b class='eur'>{eur(s['eur'])}{per_year}</b><br>"
                    f"<span class='mut'>{e(s['text'])}</span></li>" for s in rep["sparpotenziale"])
    anoms = "".join(f"<li><span class='mut'>{L('from', 'ab')} {fmt_date(date.fromisoformat(a['start_day']))}</span> "
                    f"{e(a['message'])} <span class='mut'>({e(a['state'])})</span></li>"
                    for a in rep["auffaelligkeiten"]) or \
        f"<li class='mut'>{L('No findings this month.', 'Keine Auffälligkeiten in diesem Monat.')}</li>"
    notes = []
    if not house_known:
        notes.append(L("No whole-house meter in Home Assistant: house consumption, grid import, the whole-house night "
                       "base load and the water heater cannot be measured. All figures refer to the metering plugs.",
                       "Kein Hauszähler in Home Assistant: Hausverbrauch, Netzbezug, nächtliche Grundlast des ganzen "
                       "Hauses und Durchlauferhitzer sind nicht messbar. Alle Zahlen beziehen sich auf die "
                       "Messsteckdosen.")
                     + (L(" The solar/battery share is estimated from the power fed into the house by the "
                          "battery/inverter (zero export: it covers the plugs first).",
                          " Der Solar/Batterie-Anteil ist aus der Einspeiseleistung von Speicher/Wechselrichter ins "
                          "Haus geschätzt (Nulleinspeisung: sie deckt zuerst die Steckdosen).")
                        if rep["solar_geschaetzt"] else ""))
    elif rep["hausdaten_stunden"] < rep["stunden"] * 0.9:
        pct = fmt_num(100 * rep["hausdaten_stunden"] / rep["stunden"], 0)
        notes.append(L(f"House data only available for {pct} % of the hours; costs without house data use the full "
                       f"grid price.",
                       f"Hausdaten nur für {pct} % der Stunden vorhanden; Kosten ohne Hausdaten sind mit vollem "
                       f"Netzpreis gerechnet."))
    for r in rows:
        if r["abdeckung"] < 0.9:
            pct = fmt_num(100 * r["abdeckung"], 0)
            notes.append(L(f"{r['name']}: readings for only {pct} % of the month.",
                           f"{r['name']}: Messwerte nur für {pct} % des Monats."))
    notes += rep["hinweise"]
    notes_html = "".join(f"<li>{e(n)}</li>" for n in notes) or f"<li class='mut'>{L('None.', 'Keine.')}</li>"
    other = (rep["haus_kwh"] - rep["steckdosen_kwh"]) if house_known else None
    title = report_title(cfg, rep)
    other_txt = ""
    if other is not None:
        other_txt = L(f"Not covered by metering plugs: {fmt_num(other, 0)} kWh (e.g. {e(cfg.house.dle_name)}, lights, "
                      f"stove).",
                      f"Nicht über Messsteckdosen erfasst: {fmt_num(other, 0)} kWh (u. a. {e(cfg.house.dle_name)}, "
                      f"Licht, Herd).")
    gm = rep["grundlast_median"]
    base_tail = (L(", of which not attributable to any plug: <b>", ", davon keiner Steckdose zuzuordnen: <b>")
                 + fmt_num(rep["unzugeordnet_median"], 0) + " W</b>" if rep["unzugeordnet_median"] is not None
                 else L(" – metering plugs only; without a meter the whole house cannot be measured",
                        " – nur Messsteckdosen, ohne Hauszähler nicht für das ganze Haus messbar"))
    return f"""<!doctype html><html lang="{lang()}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>{CSS}</style></head><body><main>
<h1>{e(title)}</h1>
<p class="mut">{L('Electricity price', 'Strompreis')} {eur(rep['preis'], 2)}/kWh, {L('feed-in tariff', 'Einspeisevergütung')} {eur(rep['einspeiseverguetung'], 2)}/kWh · {L('created', 'erstellt')} {e(generated)}</p>
<div class="grid">{kpi_html}</div>
<h2>{L('Consumption and cost per device', 'Verbrauch und Kosten pro Gerät')}</h2>
<div class="card tbl"><table><thead><tr><th>{L('Device', 'Gerät')}</th><th></th><th>kWh</th><th>{L('Cost', 'Kosten')}</th><th>{L('Solar/battery', 'Solar/Batterie')}</th><th>{L('Data coverage', 'Datenabdeckung')}</th></tr></thead>
<tbody>{trs}</tbody></table>
<p class="mut">{L('Cost = consumption × grid share of each hour × electricity price.', 'Kosten = Verbrauch × Netzanteil der jeweiligen Stunde × Strompreis.')} {other_txt}</p></div>
<h2>{L('Base load trend', 'Grundlast-Entwicklung')}</h2><div class="card">{_svg_baseload(rep['grundlast'], chart_label, night_label)}
<p>{L('Median at night', 'Median nachts')}: <b>{fmt_num(gm, 0)} W</b> (≈ {eur((gm or 0) * 8.76 * rep['preis'])}{per_year}){base_tail}.</p>{_baseload_trend(rep)}</div>
<h2>{L('Top 5 savings', 'Top-5-Sparpotenziale')}</h2><div class="card"><ol class="save">{saves or "<li class='mut'>" + L('No savings could be calculated.', 'Keine Sparpotenziale berechenbar.') + "</li>"}</ol></div>
<h2>{L('Findings', 'Auffälligkeiten')}</h2><div class="card"><ul>{anoms}</ul></div>
<h2>{L('Notes on the data', 'Hinweise zur Datenlage')}</h2><div class="card"><ul>{notes_html}</ul></div>
</main></body></html>"""


def _baseload_trend(rep: dict) -> str:
    vals = [b for _, b, _ in rep["grundlast"] if b is not None]
    if len(vals) < 10:
        return ""
    k = max(3, len(vals) // 3)
    first, last = median(vals[:k]), median(vals[-k:])
    diff = last - first
    return L(f"<p>Trend this month: from {fmt_num(first, 0)} W (first third) to {fmt_num(last, 0)} W (last third), "
             f"{'up' if diff > 0 else 'down'} by {fmt_num(abs(diff), 0)} W.</p>",
             f"<p>Entwicklung im Monat: von {fmt_num(first, 0)} W (erstes Drittel) auf {fmt_num(last, 0)} W "
             f"(letztes Drittel), also um {fmt_num(abs(diff), 0)} W {'gestiegen' if diff > 0 else 'gesunken'}.</p>")


def summary_text(rep: dict) -> str:
    top = rep["geraete"][0] if rep["geraete"] else None
    parts = [f"{rep['titel']}:"]
    if rep["haus_kwh"] is None:
        solar = rep["solar_anteil"]
        parts.append(L(f"metering plugs {fmt_num(rep['steckdosen_kwh'], 0)} kWh, grid cost {eur(rep['netzkosten'])}"
                       + (f", solar/battery about {fmt_num(100 * solar, 0)} %." if solar is not None else "."),
                       f"Messsteckdosen {fmt_num(rep['steckdosen_kwh'], 0)} kWh, davon Netz {eur(rep['netzkosten'])}"
                       + (f", Solar/Batterie ca. {fmt_num(100 * solar, 0)} %." if solar is not None else ".")))
    else:
        sh = fmt_num(100 * (rep["solar_anteil"] or 0), 0)
        parts.append(L(f"house {fmt_num(rep['haus_kwh'], 0)} kWh, grid {fmt_num(rep['netz_kwh'] or 0, 0)} kWh "
                       f"({eur(rep['netzkosten'])}), solar/battery {sh} %.",
                       f"Haus {fmt_num(rep['haus_kwh'], 0)} kWh, Netz {fmt_num(rep['netz_kwh'] or 0, 0)} kWh "
                       f"({eur(rep['netzkosten'])}), Solar/Batterie {sh} %."))
    if top:
        parts.append(L(f"Biggest consumer: {top['name']} {fmt_num(top['kwh'], 0)} kWh.",
                       f"Größter Verbraucher: {top['name']} {fmt_num(top['kwh'], 0)} kWh."))
    if rep["grundlast_median"]:
        parts.append(L(f"Night base load {fmt_num(rep['grundlast_median'], 0)} W.",
                       f"Grundlast nachts {fmt_num(rep['grundlast_median'], 0)} W."))
    if rep["sparpotenziale"]:
        s = rep["sparpotenziale"][0]
        parts.append(L(f"Biggest saving: {s['titel']} (≈ {eur(s['eur'])}/year).",
                       f"Größtes Sparpotenzial: {s['titel']} (≈ {eur(s['eur'])}/Jahr)."))
    return " ".join(parts)


def write_report(cfg: Config, store: Store, det: Detector, clock: Clock, year: int, month: int) -> tuple[Path, dict]:
    rep = build(cfg, store, det, clock, year, month)
    out = Path(cfg.report_dir)
    out.mkdir(parents=True, exist_ok=True)
    p = out / (L("report", "bericht") + f"-{year}-{month:02d}.html")
    p.write_text(render_html(rep, cfg, fmt_datetime(clock.local())), encoding="utf-8")
    return p, rep
