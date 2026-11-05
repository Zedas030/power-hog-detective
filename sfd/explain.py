"""Plain-language (German) explanations for every kind of finding."""
from __future__ import annotations

from .util import WEEKDAYS, fmt_clock, fmt_minutes, fmt_num

METRIC_LABEL = {
    "zyklus_min": "Zyklusdauer",
    "pause_min": "Pause zwischen Zyklen",
    "zyklen": "Zyklen pro Tag",
    "laufanteil": "Laufzeitanteil",
    "kwh": "Tagesverbrauch",
    "grundlast_w": "Grundlast",
    "standby_w": "Standby-Verbrauch",
    "ein_min": "Einschaltzeit",
    "betrieb_w": "Leistung im Betrieb",
    "nacht_w": "Nächtliche Grundlast",
    "unzugeordnet_nacht_w": "Nicht zugeordnete Grundlast",
    "laufzeit": "Laufzeit",
    "null": "Kein Verbrauch",
    "keine_daten": "Keine Messwerte",
    "nicht_verfuegbar": "Nicht erreichbar",
    "entitaet": "Entität",
    "ha": "Home Assistant",
    "hausdaten": "Hausdaten",
}

PROFILES = ("kuehlgeraet", "boiler", "spuelmaschine", "waschmaschine", "trockner", "drucker", "allgemein")


def profile_for(name: str, explicit: str | None = None) -> str:
    if explicit in PROFILES:
        return explicit
    n = name.lower()
    if any(k in n for k in ("kühl", "kuehl", "gefrier", "frost")):
        return "kuehlgeraet"
    if any(k in n for k in ("boiler", "speicher", "warmwasser")):
        return "boiler"
    if "spül" in n or "spuel" in n:
        return "spuelmaschine"
    if "wasch" in n:
        return "waschmaschine"
    if "trockn" in n:
        return "trockner"
    if "druck" in n:
        return "drucker"
    return "allgemein"


def _pct(cur, base):
    if not base:
        return "deutlich"
    return f"{fmt_num(abs(cur - base) / abs(base) * 100, 0)} %"


def _eur_year_w(diff_w, price):
    return fmt_num(abs(diff_w) * 8.76 * price, 0)


def _since(n_days: int, start_day) -> str:
    if n_days <= 7 and start_day is not None:
        return f"seit {WEEKDAYS[start_day.weekday()]}" if n_days > 1 else "seit gestern"
    return f"seit {n_days} Tagen"


def daily_message(name: str, typ: str, profile: str, metric: str, direction: int, cur: float, base: float,
                  n_days: int, start_day, price: float, extra: dict | None = None) -> str:
    """One sentence for a stable daily deviation."""
    extra = extra or {}
    seit = f"seit {n_days} Tagen"
    more = direction > 0
    if metric == "zyklus_min":
        if profile == "kuehlgeraet":
            if more:
                return (f"{name}: Kompressor läuft {seit} {_pct(cur, base)} länger pro Zyklus "
                        f"({fmt_minutes(cur)} statt {fmt_minutes(base)}) – Tür, Dichtung oder Vereisung prüfen.")
            return (f"{name}: Kompressor-Zyklen {seit} {_pct(cur, base)} kürzer ({fmt_minutes(cur)} statt "
                    f"{fmt_minutes(base)}) – häufiges Takten: Thermostat oder Kältekreislauf prüfen.")
        if profile == "boiler":
            if more:
                return (f"{name}: Heizphasen {seit} {_pct(cur, base)} länger ({fmt_minutes(cur)} statt "
                        f"{fmt_minutes(base)}) – Heizstab verkalkt oder mehr Warmwasser gezapft?")
            return (f"{name}: Heizphasen {seit} {_pct(cur, base)} kürzer ({fmt_minutes(cur)} statt "
                    f"{fmt_minutes(base)}) – Thermostat oder Heizstab prüfen.")
        word = "länger" if more else "kürzer"
        return (f"{name}: Zyklen dauern {seit} {_pct(cur, base)} {word} ({fmt_minutes(cur)} statt "
                f"{fmt_minutes(base)}).")
    if metric == "pause_min":
        if profile == "kuehlgeraet":
            return (f"{name}: Pausen zwischen den Kühlzyklen {seit} {_pct(cur, base)} kürzer ({fmt_minutes(cur)} "
                    f"statt {fmt_minutes(base)}) – es kommt mehr Wärme rein: Tür, Dichtung, warme Umgebung "
                    f"oder viel frisch eingeräumt.")
        if profile == "boiler":
            return (f"{name}: heizt {seit} öfter nach (Pause {fmt_minutes(cur)} statt {fmt_minutes(base)}) – "
                    f"mehr Warmwasser gezapft, tropfender Hahn oder Wärmeverlust am Speicher?")
        return (f"{name}: Pausen zwischen den Zyklen {seit} {_pct(cur, base)} kürzer "
                f"({fmt_minutes(cur)} statt {fmt_minutes(base)}).")
    if metric == "zyklen":
        return (f"{name}: {seit} {_pct(cur, base)} mehr Zyklen pro Tag ({fmt_num(cur)} statt {fmt_num(base)})"
                + (" – Tür, Dichtung oder Umgebungstemperatur prüfen." if profile == "kuehlgeraet" else "."))
    if metric == "laufanteil":
        return (f"{name}: läuft {seit} {_pct(cur, base)} mehr der Zeit ({fmt_num(cur * 100)} % statt "
                f"{fmt_num(base * 100)} % des Tages).")
    if metric == "kwh":
        diff = cur - base
        return (f"{name}: verbraucht {seit} {_pct(cur, base)} mehr ({fmt_num(cur, 2)} statt {fmt_num(base, 2)} "
                f"kWh pro Tag, etwa {fmt_num(diff * 365 * price, 0)} € pro Jahr mehr).")
    if metric == "grundlast_w":
        diff = cur - base
        if more:
            return (f"{name}: Grundlast {_since(n_days, start_day)} +{fmt_num(diff)} W ({fmt_num(cur, 1)} W statt "
                    f"{fmt_num(base, 1)} W, ≈ {_eur_year_w(diff, price)} €/Jahr) – zusätzliches Gerät "
                    f"angeschlossen, Lüfter, Netzteil oder Dauerlast durch Software prüfen.")
        return (f"{name}: Grundlast {_since(n_days, start_day)} −{fmt_num(-diff)} W ({fmt_num(cur, 1)} W statt "
                f"{fmt_num(base, 1)} W) – läuft das Gerät noch normal (Teilausfall, etwas abgesteckt)?")
    if metric == "standby_w":
        diff = cur - base
        if typ == "ladegeraet":
            return (f"{name}: zieht ohne Ladevorgang {seit} {fmt_num(cur, 1)} W statt {fmt_num(base, 1)} W "
                    f"(≈ {_eur_year_w(diff, price)} €/Jahr mehr) – hängt ein Gerät dauerhaft dran oder ist das "
                    f"Netzteil defekt?")
        return (f"{name}: Standby-Verbrauch {seit} {fmt_num(cur, 1)} W statt {fmt_num(base, 1)} W "
                f"(+{fmt_num(diff, 1)} W ≈ {_eur_year_w(diff, price)} €/Jahr) – Schnellstart, Updates oder "
                f"Netzwerk-Standby aktiv?")
    if metric == "ein_min":
        delta = cur - base
        word = "später" if delta > 0 else "früher"
        return (f"{name}: schaltet {seit} um {fmt_clock(cur)} statt um {fmt_clock(base)} ein "
                f"({fmt_num(abs(delta))} Min. {word}) – Zeitschaltuhr verstellt oder Zeitumstellung?")
    if metric == "betrieb_w":
        word = "mehr" if more else "weniger"
        return (f"{name}: braucht im Betrieb {seit} {_pct(cur, base)} {word} ({fmt_num(cur)} W statt "
                f"{fmt_num(base)} W) – "
                + ("zusätzliches Gerät angeschlossen?" if more else "Lampe oder Lüfter ausgefallen?"))
    if metric == "nacht_w":
        diff = cur - base
        un = extra.get("unzugeordnet_diff")
        tail = ""
        if un is not None:
            if un >= 0.6 * diff:
                tail = ", keiner Steckdose zuzuordnen"
            else:
                tail = f", davon {fmt_num(max(diff - un, 0))} W an den Messsteckdosen"
        return (f"Nächtliche Grundlast {_since(n_days, start_day)} {'+' if diff >= 0 else '−'}{fmt_num(abs(diff))} W "
                f"({fmt_num(cur)} W statt {fmt_num(base)} W){tail} – ≈ {_eur_year_w(diff, price)} €/Jahr.")
    if metric == "unzugeordnet_nacht_w":
        diff = cur - base
        return (f"Nächtliche Grundlast {_since(n_days, start_day)} {'+' if diff >= 0 else '−'}{fmt_num(abs(diff))} W, "
                f"keiner Steckdose zuzuordnen ({fmt_num(cur)} W statt {fmt_num(base)} W ohne Messsteckdose, "
                f"≈ {_eur_year_w(diff, price)} €/Jahr) – Geräte ohne Messsteckdose prüfen "
                f"(Umwälzpumpe, Heizlüfter, Netzteile, Aquarium …).")
    label = METRIC_LABEL.get(metric, metric)
    return f"{name}: {label} {seit} auffällig ({fmt_num(cur, 1)} statt {fmt_num(base, 1)})."


def program_message(name: str, profile: str, program: str, what: str, cur: float, base: float, n_runs: int) -> str:
    if what == "dauer":
        detail = f"{fmt_minutes(cur)} statt {fmt_minutes(base)}"
        head = f"{name}: {program} dauert seit {n_runs} Läufen {_pct(cur, base)} länger ({detail})"
    else:
        detail = f"{fmt_num(cur, 2)} statt {fmt_num(base, 2)} kWh"
        head = f"{name}: {program} braucht seit {n_runs} Läufen {_pct(cur, base)} mehr Energie ({detail})"
    hint = {
        "spuelmaschine": "Heizstab verkalkt, Sieb verstopft oder Salz/Klarspüler leer?",
        "waschmaschine": "Heizstab verkalkt, Flusensieb oder Pumpe prüfen.",
        "trockner": "Flusensieb oder Wärmetauscher reinigen.",
        "drucker": "Druckbett-Heizung, Umgebungstemperatur oder andere Druckeinstellungen?",
        "boiler": "Heizstab verkalkt?",
    }.get(profile, "Verschleiß oder geänderte Einstellungen prüfen.")
    return f"{head} – {hint}"


def runtime_message(name: str, profile: str, running_s: float, limit_s: float, configured: bool = False) -> str:
    dur, lim = fmt_minutes(running_s / 60), fmt_minutes(limit_s / 60)
    grenze = f"eingestellte Grenze {lim}" if configured else f"sonst höchstens {lim}"
    if profile == "kuehlgeraet":
        return f"{name}: Kompressor läuft seit {dur} ohne Pause ({grenze}) – Tür offen oder vereist?"
    return f"{name} läuft seit {dur} ununterbrochen ({grenze}) – vergessen auszuschalten?"


def zero_message(name: str, zero_s: float, has_switch: bool) -> str:
    sw = ", obwohl die Steckdose eingeschaltet ist" if has_switch else ""
    return (f"{name} verbraucht seit {fmt_minutes(zero_s / 60)} gar nichts mehr{sw} – Gerät defekt, Netzteil "
            f"oder Gerätesicherung prüfen.")


def stale_message(name: str, age_s: float) -> str:
    return (f"{name}: Steckdose meldet seit {fmt_minutes(age_s / 60)} keine Werte mehr – WLAN, Strom an der "
            f"Dose oder TP-Link-Integration prüfen.")


def unavailable_message(name: str, age_s: float, state: str) -> str:
    return (f"{name}: Steckdose ist seit {fmt_minutes(age_s / 60)} in Home Assistant „{state}“ – WLAN, Strom an "
            f"der Dose oder TP-Link-Integration prüfen.")


def recovery_message(name: str, metric: str) -> str:
    return f"{name}: {METRIC_LABEL.get(metric, metric)} ist wieder normal."


def learned_message(name: str, metric: str, n_days: int) -> str:
    return (f"{name}: Die Abweichung bei „{METRIC_LABEL.get(metric, metric)}“ hält seit {n_days} Tagen an – "
            f"ich lerne das jetzt als neues Normal.")
