"""Plain-language explanations (English or German) for every kind of finding."""
from __future__ import annotations

from .i18n import L, fmt_minutes, fmt_num, weekday
from .util import fmt_clock

_LABELS = {
    "zyklus_min": ("Cycle length", "Zyklusdauer"),
    "pause_min": ("Pause between cycles", "Pause zwischen Zyklen"),
    "zyklen": ("Cycles per day", "Zyklen pro Tag"),
    "laufanteil": ("Share of running time", "Laufzeitanteil"),
    "kwh": ("Daily consumption", "Tagesverbrauch"),
    "grundlast_w": ("Base load", "Grundlast"),
    "standby_w": ("Standby consumption", "Standby-Verbrauch"),
    "ein_min": ("Switch-on time", "Einschaltzeit"),
    "betrieb_w": ("Power while running", "Leistung im Betrieb"),
    "nacht_w": ("Night base load", "Nächtliche Grundlast"),
    "unzugeordnet_nacht_w": ("Unassigned base load", "Nicht zugeordnete Grundlast"),
    "laufzeit": ("Running time", "Laufzeit"),
    "null": ("No consumption", "Kein Verbrauch"),
    "keine_daten": ("No readings", "Keine Messwerte"),
    "nicht_verfuegbar": ("Unavailable", "Nicht erreichbar"),
    "entitaet": ("Entity", "Entität"),
    "ha": ("Home Assistant", "Home Assistant"),
    "hausdaten": ("House data", "Hausdaten"),
}


def label(metric: str) -> str:
    en, de = _LABELS.get(metric, (metric, metric))
    return L(en, de)


class _Labels(dict):
    """dict-like view so callers can keep using METRIC_LABEL.get(metric, metric)."""

    def get(self, key, default=None):
        return label(key) if key in _LABELS else default

    def __getitem__(self, key):
        return label(key)

    def __contains__(self, key):
        return key in _LABELS


METRIC_LABEL = _Labels()

# internal profile ids (German) with English aliases accepted in config.yaml
PROFILES = ("kuehlgeraet", "boiler", "spuelmaschine", "waschmaschine", "trockner", "drucker", "allgemein")
PROFILE_ALIASES = {"fridge": "kuehlgeraet", "freezer": "kuehlgeraet", "water_heater": "boiler",
                   "dishwasher": "spuelmaschine", "washing_machine": "waschmaschine", "dryer": "trockner",
                   "printer": "drucker", "3d_printer": "drucker", "generic": "allgemein"}


def profile_for(name: str, explicit: str | None = None) -> str:
    explicit = PROFILE_ALIASES.get(explicit, explicit)
    if explicit in PROFILES:
        return explicit
    n = name.lower()
    if any(k in n for k in ("kühl", "kuehl", "gefrier", "frost", "fridge", "freezer")):
        return "kuehlgeraet"
    if any(k in n for k in ("boiler", "speicher", "warmwasser", "water heater")):
        return "boiler"
    if any(k in n for k in ("spül", "spuel", "dishwasher")):
        return "spuelmaschine"
    if any(k in n for k in ("wasch", "washing")):
        return "waschmaschine"
    if any(k in n for k in ("trockn", "dryer")):
        return "trockner"
    if any(k in n for k in ("druck", "printer")):
        return "drucker"
    return "allgemein"


def _pct(cur, base):
    if not base:
        return L("clearly", "deutlich")
    return f"{fmt_num(abs(cur - base) / abs(base) * 100, 0)} %"


def _eur_year_w(diff_w, price):
    return fmt_num(abs(diff_w) * 8.76 * price, 0)


def _since(n_days: int, start_day) -> str:
    if n_days <= 7 and start_day is not None:
        if n_days > 1:
            return L(f"since {weekday(start_day.weekday())}", f"seit {weekday(start_day.weekday())}")
        return L("since yesterday", "seit gestern")
    return L(f"for {n_days} days", f"seit {n_days} Tagen")


def daily_message(name: str, typ: str, profile: str, metric: str, direction: int, cur: float, base: float,
                  n_days: int, start_day, price: float, extra: dict | None = None) -> str:
    """One sentence for a stable daily deviation."""
    extra = extra or {}
    seit = L(f"for {n_days} days", f"seit {n_days} Tagen")
    more = direction > 0
    pct = _pct(cur, base)
    c_min, b_min = fmt_minutes(cur), fmt_minutes(base)
    if metric == "zyklus_min":
        if profile == "kuehlgeraet":
            if more:
                return L(f"{name}: compressor has been running {pct} longer per cycle {seit} ({c_min} instead of "
                         f"{b_min}) – check the door, the seal or for icing.",
                         f"{name}: Kompressor läuft {seit} {pct} länger pro Zyklus ({c_min} statt {b_min}) – "
                         f"Tür, Dichtung oder Vereisung prüfen.")
            return L(f"{name}: compressor cycles {pct} shorter {seit} ({c_min} instead of {b_min}) – short cycling: "
                     f"check the thermostat or the refrigerant circuit.",
                     f"{name}: Kompressor-Zyklen {seit} {pct} kürzer ({c_min} statt {b_min}) – häufiges Takten: "
                     f"Thermostat oder Kältekreislauf prüfen.")
        if profile == "boiler":
            if more:
                return L(f"{name}: heating phases {pct} longer {seit} ({c_min} instead of {b_min}) – scaled heating "
                         f"element or more hot water drawn?",
                         f"{name}: Heizphasen {seit} {pct} länger ({c_min} statt {b_min}) – Heizstab verkalkt oder "
                         f"mehr Warmwasser gezapft?")
            return L(f"{name}: heating phases {pct} shorter {seit} ({c_min} instead of {b_min}) – check the "
                     f"thermostat or the heating element.",
                     f"{name}: Heizphasen {seit} {pct} kürzer ({c_min} statt {b_min}) – Thermostat oder Heizstab "
                     f"prüfen.")
        return L(f"{name}: cycles {pct} {'longer' if more else 'shorter'} {seit} ({c_min} instead of {b_min}).",
                 f"{name}: Zyklen dauern {seit} {pct} {'länger' if more else 'kürzer'} ({c_min} statt {b_min}).")
    if metric == "pause_min":
        if profile == "kuehlgeraet":
            return L(f"{name}: pauses between cooling cycles {pct} shorter {seit} ({c_min} instead of {b_min}) – "
                     f"more heat gets in: door, seal, warm surroundings or a lot of fresh food.",
                     f"{name}: Pausen zwischen den Kühlzyklen {seit} {pct} kürzer ({c_min} statt {b_min}) – es kommt "
                     f"mehr Wärme rein: Tür, Dichtung, warme Umgebung oder viel frisch eingeräumt.")
        if profile == "boiler":
            return L(f"{name}: reheats more often {seit} (pause {c_min} instead of {b_min}) – more hot water used, "
                     f"a dripping tap or heat loss at the tank?",
                     f"{name}: heizt {seit} öfter nach (Pause {c_min} statt {b_min}) – mehr Warmwasser gezapft, "
                     f"tropfender Hahn oder Wärmeverlust am Speicher?")
        return L(f"{name}: pauses between cycles {pct} shorter {seit} ({c_min} instead of {b_min}).",
                 f"{name}: Pausen zwischen den Zyklen {seit} {pct} kürzer ({c_min} statt {b_min}).")
    if metric == "zyklen":
        tip = L(" – check the door, the seal or the room temperature.",
                " – Tür, Dichtung oder Umgebungstemperatur prüfen.") if profile == "kuehlgeraet" else "."
        return L(f"{name}: {pct} more cycles per day {seit} ({fmt_num(cur)} instead of {fmt_num(base)})",
                 f"{name}: {seit} {pct} mehr Zyklen pro Tag ({fmt_num(cur)} statt {fmt_num(base)})") + tip
    if metric == "laufanteil":
        return L(f"{name}: running {pct} more of the time {seit} ({fmt_num(cur * 100)} % instead of "
                 f"{fmt_num(base * 100)} % of the day).",
                 f"{name}: läuft {seit} {pct} mehr der Zeit ({fmt_num(cur * 100)} % statt {fmt_num(base * 100)} % "
                 f"des Tages).")
    if metric == "kwh":
        eur = fmt_num((cur - base) * 365 * price, 0)
        return L(f"{name}: using {pct} more {seit} ({fmt_num(cur, 2)} instead of {fmt_num(base, 2)} kWh per day, "
                 f"about €{eur} more per year).",
                 f"{name}: verbraucht {seit} {pct} mehr ({fmt_num(cur, 2)} statt {fmt_num(base, 2)} kWh pro Tag, "
                 f"etwa {eur} € pro Jahr mehr).")
    if metric == "grundlast_w":
        diff = cur - base
        since = _since(n_days, start_day)
        if more:
            return L(f"{name}: base load +{fmt_num(diff)} W {since} ({fmt_num(cur, 1)} W instead of {fmt_num(base, 1)} W,"
                     f" ≈ €{_eur_year_w(diff, price)}/year) – another device plugged in, a fan, a power supply or "
                     f"software keeping it busy?",
                     f"{name}: Grundlast {since} +{fmt_num(diff)} W ({fmt_num(cur, 1)} W statt {fmt_num(base, 1)} W, "
                     f"≈ {_eur_year_w(diff, price)} €/Jahr) – zusätzliches Gerät angeschlossen, Lüfter, Netzteil "
                     f"oder Dauerlast durch Software prüfen.")
        return L(f"{name}: base load −{fmt_num(-diff)} W {since} ({fmt_num(cur, 1)} W instead of {fmt_num(base, 1)} W) "
                 f"– is the device still working normally (partial failure, something unplugged)?",
                 f"{name}: Grundlast {since} −{fmt_num(-diff)} W ({fmt_num(cur, 1)} W statt {fmt_num(base, 1)} W) – "
                 f"läuft das Gerät noch normal (Teilausfall, etwas abgesteckt)?")
    if metric == "standby_w":
        diff = cur - base
        if typ == "ladegeraet":
            return L(f"{name}: draws {fmt_num(cur, 1)} W without charging {seit} instead of {fmt_num(base, 1)} W "
                     f"(≈ €{_eur_year_w(diff, price)}/year more) – is a device permanently attached or the power "
                     f"supply faulty?",
                     f"{name}: zieht ohne Ladevorgang {seit} {fmt_num(cur, 1)} W statt {fmt_num(base, 1)} W "
                     f"(≈ {_eur_year_w(diff, price)} €/Jahr mehr) – hängt ein Gerät dauerhaft dran oder ist das "
                     f"Netzteil defekt?")
        return L(f"{name}: standby consumption {fmt_num(cur, 1)} W instead of {fmt_num(base, 1)} W {seit} "
                 f"(+{fmt_num(diff, 1)} W ≈ €{_eur_year_w(diff, price)}/year) – quick start, updates or network "
                 f"standby enabled?",
                 f"{name}: Standby-Verbrauch {seit} {fmt_num(cur, 1)} W statt {fmt_num(base, 1)} W "
                 f"(+{fmt_num(diff, 1)} W ≈ {_eur_year_w(diff, price)} €/Jahr) – Schnellstart, Updates oder "
                 f"Netzwerk-Standby aktiv?")
    if metric == "ein_min":
        delta = cur - base
        return L(f"{name}: switches on at {fmt_clock(cur)} instead of {fmt_clock(base)} {seit} "
                 f"({fmt_num(abs(delta))} min {'later' if delta > 0 else 'earlier'}) – timer changed or "
                 f"daylight saving time?",
                 f"{name}: schaltet {seit} um {fmt_clock(cur)} statt um {fmt_clock(base)} ein "
                 f"({fmt_num(abs(delta))} Min. {'später' if delta > 0 else 'früher'}) – Zeitschaltuhr verstellt "
                 f"oder Zeitumstellung?")
    if metric == "betrieb_w":
        return L(f"{name}: uses {pct} {'more' if more else 'less'} while running {seit} ({fmt_num(cur)} W instead of "
                 f"{fmt_num(base)} W) – " + ("another device plugged in?" if more else "lamp or fan failed?"),
                 f"{name}: braucht im Betrieb {seit} {pct} {'mehr' if more else 'weniger'} ({fmt_num(cur)} W statt "
                 f"{fmt_num(base)} W) – " + ("zusätzliches Gerät angeschlossen?" if more
                                             else "Lampe oder Lüfter ausgefallen?"))
    if metric == "nacht_w":
        diff = cur - base
        un = extra.get("unzugeordnet_diff")
        tail = ""
        if un is not None:
            if un >= 0.6 * diff:
                tail = L(", not attributable to any plug", ", keiner Steckdose zuzuordnen")
            else:
                tail = L(f", of which {fmt_num(max(diff - un, 0))} W at the metered plugs",
                         f", davon {fmt_num(max(diff - un, 0))} W an den Messsteckdosen")
        sign = "+" if diff >= 0 else "−"
        return L(f"Night base load {sign}{fmt_num(abs(diff))} W {_since(n_days, start_day)} ({fmt_num(cur)} W instead "
                 f"of {fmt_num(base)} W){tail} – ≈ €{_eur_year_w(diff, price)}/year.",
                 f"Nächtliche Grundlast {_since(n_days, start_day)} {sign}{fmt_num(abs(diff))} W ({fmt_num(cur)} W "
                 f"statt {fmt_num(base)} W){tail} – ≈ {_eur_year_w(diff, price)} €/Jahr.")
    if metric == "unzugeordnet_nacht_w":
        diff = cur - base
        sign = "+" if diff >= 0 else "−"
        return L(f"Night base load {sign}{fmt_num(abs(diff))} W {_since(n_days, start_day)}, not attributable to any "
                 f"plug ({fmt_num(cur)} W instead of {fmt_num(base)} W without a metering plug, "
                 f"≈ €{_eur_year_w(diff, price)}/year) – check devices without a metering plug (circulation pump, "
                 f"fan heater, power supplies, aquarium …).",
                 f"Nächtliche Grundlast {_since(n_days, start_day)} {sign}{fmt_num(abs(diff))} W, keiner Steckdose "
                 f"zuzuordnen ({fmt_num(cur)} W statt {fmt_num(base)} W ohne Messsteckdose, "
                 f"≈ {_eur_year_w(diff, price)} €/Jahr) – Geräte ohne Messsteckdose prüfen (Umwälzpumpe, "
                 f"Heizlüfter, Netzteile, Aquarium …).")
    return L(f"{name}: {label(metric)} unusual {seit} ({fmt_num(cur, 1)} instead of {fmt_num(base, 1)}).",
             f"{name}: {label(metric)} {seit} auffällig ({fmt_num(cur, 1)} statt {fmt_num(base, 1)}).")


def program_name(pid: str) -> str:
    return L(f"Program {pid}", f"Programm {pid}")


def program_message(name: str, profile: str, program: str, what: str, cur: float, base: float, n_runs: int) -> str:
    pct = _pct(cur, base)
    if what == "dauer":
        head = L(f"{name}: {program} has taken {pct} longer for {n_runs} runs ({fmt_minutes(cur)} instead of "
                 f"{fmt_minutes(base)})",
                 f"{name}: {program} dauert seit {n_runs} Läufen {pct} länger ({fmt_minutes(cur)} statt "
                 f"{fmt_minutes(base)})")
    else:
        head = L(f"{name}: {program} has needed {pct} more energy for {n_runs} runs ({fmt_num(cur, 2)} instead of "
                 f"{fmt_num(base, 2)} kWh)",
                 f"{name}: {program} braucht seit {n_runs} Läufen {pct} mehr Energie ({fmt_num(cur, 2)} statt "
                 f"{fmt_num(base, 2)} kWh)")
    hints = {
        "spuelmaschine": ("scaled heating element, clogged filter or salt/rinse aid empty?",
                          "Heizstab verkalkt, Sieb verstopft oder Salz/Klarspüler leer?"),
        "waschmaschine": ("check for a scaled heating element, the lint filter or the pump.",
                          "Heizstab verkalkt, Flusensieb oder Pumpe prüfen."),
        "trockner": ("clean the lint filter or the heat exchanger.", "Flusensieb oder Wärmetauscher reinigen."),
        "drucker": ("bed heater, room temperature or different print settings?",
                    "Druckbett-Heizung, Umgebungstemperatur oder andere Druckeinstellungen?"),
        "boiler": ("scaled heating element?", "Heizstab verkalkt?"),
    }
    en, de = hints.get(profile, ("check for wear or changed settings.", "Verschleiß oder geänderte Einstellungen prüfen."))
    return f"{head} – {L(en, de)}"


def runtime_message(name: str, profile: str, running_s: float, limit_s: float, configured: bool = False) -> str:
    dur, lim = fmt_minutes(running_s / 60), fmt_minutes(limit_s / 60)
    grenze = L(f"configured limit {lim}" if configured else f"usually at most {lim}",
               f"eingestellte Grenze {lim}" if configured else f"sonst höchstens {lim}")
    if profile == "kuehlgeraet":
        return L(f"{name}: compressor has been running without a pause for {dur} ({grenze}) – door open or iced up?",
                 f"{name}: Kompressor läuft seit {dur} ohne Pause ({grenze}) – Tür offen oder vereist?")
    return L(f"{name} has been running continuously for {dur} ({grenze}) – forgot to switch it off?",
             f"{name} läuft seit {dur} ununterbrochen ({grenze}) – vergessen auszuschalten?")


def zero_message(name: str, zero_s: float, has_switch: bool) -> str:
    d = fmt_minutes(zero_s / 60)
    return L(f"{name} has used no power at all for {d}" + (" although the plug is switched on" if has_switch else "")
             + " – check the device, its power supply or its fuse.",
             f"{name} verbraucht seit {d} gar nichts mehr" + (", obwohl die Steckdose eingeschaltet ist"
                                                               if has_switch else "")
             + " – Gerät defekt, Netzteil oder Gerätesicherung prüfen.")


def stale_message(name: str, age_s: float) -> str:
    d = fmt_minutes(age_s / 60)
    return L(f"{name}: plug has sent no new readings for {d} – check Wi-Fi, power at the socket or the integration.",
             f"{name}: Steckdose meldet seit {d} keine Werte mehr – WLAN, Strom an der Dose oder TP-Link-Integration "
             f"prüfen.")


def unavailable_message(name: str, age_s: float, state: str) -> str:
    d = fmt_minutes(age_s / 60)
    return L(f"{name}: plug has been “{state}” in Home Assistant for {d} – check Wi-Fi, power at the socket or the "
             f"integration.",
             f"{name}: Steckdose ist seit {d} in Home Assistant „{state}“ – WLAN, Strom an der Dose oder "
             f"TP-Link-Integration prüfen.")


def recovery_message(name: str, metric: str) -> str:
    return L(f"{name}: {label(metric)} is back to normal.", f"{name}: {label(metric)} ist wieder normal.")


def learned_message(name: str, metric: str, n_days: int) -> str:
    return L(f"{name}: the change in “{label(metric)}” has lasted {n_days} days – learning it as the new normal.",
             f"{name}: Die Abweichung bei „{label(metric)}“ hält seit {n_days} Tagen an – ich lerne das jetzt als "
             f"neues Normal.")
