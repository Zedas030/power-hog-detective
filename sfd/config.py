"""Load and validate the commented YAML configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .util import parse_duration

TYPES = ("zyklus", "dauerlast", "programm", "nutzung", "ladegeraet", "zeitplan")

TYPE_LABEL = {
    "zyklus": "Zyklus-Gerät",
    "dauerlast": "Dauerlast-Gerät",
    "programm": "Programm-Gerät",
    "nutzung": "Nutzungsgerät",
    "ladegeraet": "Ladegerät",
    "zeitplan": "Zeitplan-Gerät",
}

# Per type: on-threshold (W), gap that still belongs to the same run, minimum run length,
# metrics evaluated daily, whether a learned runtime limit applies, default "zero power" alarm.
TYPE_DEFAULTS = {
    "zyklus": dict(an_w=15.0, luecke=90, min_lauf=60, auto_laufzeit=True, null_nach=None),
    "dauerlast": dict(an_w=0.5, luecke=60, min_lauf=60, auto_laufzeit=False, null_nach=15 * 60),
    "programm": dict(an_w=5.0, luecke=20 * 60, min_lauf=5 * 60, auto_laufzeit=True, null_nach=None),
    "nutzung": dict(an_w=15.0, luecke=5 * 60, min_lauf=2 * 60, auto_laufzeit=True, null_nach=None),
    "ladegeraet": dict(an_w=4.0, luecke=5 * 60, min_lauf=5 * 60, auto_laufzeit=False, null_nach=None),
    "zeitplan": dict(an_w=10.0, luecke=10 * 60, min_lauf=10 * 60, auto_laufzeit=True, null_nach=None),
}


@dataclass
class Device:
    key: str
    name: str
    typ: str
    leistung: str
    schalter: str | None = None
    an_w: float = 10.0
    luecke: float = 60
    min_lauf: float = 60
    max_laufzeit: float | None = None
    auto_laufzeit: bool = True
    null_nach: float | None = None
    verlauf: list = field(default_factory=list)
    hinweis: str | None = None
    aktiv: bool = True
    profil: str | None = None
    programme_pruefen: bool = True
    lebenszeichen: str | None = None


@dataclass
class House:
    hausverbrauch: str | None = None
    netzbezug: str | None = None
    einspeisung: str | None = None
    nacht_von: int = 60  # minute of day
    nacht_bis: int = 300
    dle_aktiv: bool = True
    dle_schwelle_w: float = 3000.0
    dle_name: str = "Durchlauferhitzer"
    verlauf: dict = field(default_factory=dict)
    solar: list = field(default_factory=list)  # power that PV/battery feed into the house (e.g. NOAH output)


@dataclass
class Config:
    ha_url: str
    ha_token: str
    notify: str
    persistent_too: bool
    prometheus_url: str | None
    web_port: int
    tz: str
    price_import: float
    price_export: float
    min_days: int
    window_days: int
    max_excluded_days: int
    min_days_after_feedback: int
    prelearn_days: int
    stable_days: int
    rel_threshold: float
    z_threshold: float
    normal_days_to_clear: int
    notify_recovery: bool
    program_deviation: float
    program_stable_runs: int
    stale_after: float
    stale_notify: bool
    unavailable_after: float
    ha_down_after: float
    house_stale_after: float
    poll_interval: float
    registry_interval: float
    db_path: str
    flush_interval: float
    raw_days: int
    minute_days: int
    quarter_days: int
    report_day: int
    report_time: tuple[int, int]
    report_dir: str
    fridge_reference_kwh: float
    dle_saving_share: float
    sensor_prefix: str
    house: House
    devices: dict[str, Device]
    raw: dict = field(default_factory=dict)

    def device(self, key: str) -> Device:
        return self.devices[key]

    def entity_map(self) -> dict[str, str]:
        """entity_id -> device key ('_haus:<role>' for house sensors)."""
        m = {}
        for d in self.devices.values():
            if d.aktiv:
                m[d.leistung] = d.key
        for role in ("hausverbrauch", "netzbezug", "einspeisung"):
            ent = getattr(self.house, role)
            if ent:
                m[ent] = f"_haus:{role}"
        return m

    def switch_map(self) -> dict[str, str]:
        return {d.schalter: d.key for d in self.devices.values() if d.aktiv and d.schalter}


def _hm(s: str) -> int:
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def load_env(path: str | os.PathLike | None) -> None:
    if not path or not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | os.PathLike, env_path: str | os.PathLike | None = None,
                require_token: bool = True) -> Config:
    load_env(env_path)
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    g = lambda *keys, default=None: _get(raw, keys, default)  # noqa: E731

    token = os.environ.get("HA_TOKEN", "")
    if require_token and not token:
        raise SystemExit("HA_TOKEN fehlt: bitte in der .env eintragen (siehe README, Abschnitt 'Secrets').")

    devices = {}
    for key, d in (raw.get("geraete") or {}).items():
        typ = d.get("typ")
        if typ not in TYPES:
            raise SystemExit(f"Gerät {key}: typ muss einer von {', '.join(TYPES)} sein, nicht {typ!r}")
        if not d.get("leistung"):
            raise SystemExit(f"Gerät {key}: 'leistung' (Leistungs-Entität in W) fehlt")
        td = TYPE_DEFAULTS[typ]
        devices[key] = Device(
            key=key,
            name=d.get("name", key),
            typ=typ,
            leistung=d["leistung"],
            schalter=d.get("schalter"),
            an_w=float(d.get("an_schwelle_w", td["an_w"])),
            luecke=parse_duration(d.get("luecke", td["luecke"])),
            min_lauf=parse_duration(d.get("min_lauf", td["min_lauf"])),
            max_laufzeit=parse_duration(d.get("max_laufzeit")),
            auto_laufzeit=bool(d.get("auto_laufzeit", td["auto_laufzeit"])),
            null_nach=parse_duration(d.get("null_meldung_nach", td["null_nach"])),
            verlauf=list(d.get("verlauf") or []),
            hinweis=d.get("hinweis"),
            aktiv=bool(d.get("aktiv", True)),
            profil=d.get("profil"),
            programme_pruefen=bool(d.get("programme_pruefen", True)),
            lebenszeichen=d.get("lebenszeichen"),
        )

    h = raw.get("haus") or {}
    dle = h.get("durchlauferhitzer") or {}
    house = House(
        hausverbrauch=h.get("hausverbrauch"),
        netzbezug=h.get("netzbezug"),
        einspeisung=h.get("einspeisung"),
        nacht_von=_hm(h.get("nacht_von", "01:00")),
        nacht_bis=_hm(h.get("nacht_bis", "05:00")),
        dle_aktiv=bool(dle.get("aktiv", True)),
        dle_schwelle_w=float(dle.get("schwelle_w", 3000)),
        dle_name=dle.get("name", "Durchlauferhitzer"),
        verlauf=dict(h.get("verlauf") or {}),
        solar=[x for x in (h.get("solar_ins_haus") or []) if x],
    )

    rt = str(g("bericht", "uhrzeit", default="07:00")).split(":")
    return Config(
        ha_url=str(g("home_assistant", "url", default="http://127.0.0.1:8123")).rstrip("/"),
        ha_token=token,
        notify=g("home_assistant", "notify", default="notify.notify"),
        persistent_too=bool(g("home_assistant", "zusaetzlich_persistent", default=False)),
        prometheus_url=(g("prometheus", "url", default=None) or None),
        web_port=int(g("web", "port", default=8101)),
        tz=g("zeitzone", default="Europe/Berlin"),
        price_import=float(g("preise", "bezug_eur_kwh", default=0.34)),
        price_export=float(g("preise", "einspeisung_eur_kwh", default=0.0)),
        min_days=int(g("lernen", "min_tage", default=14)),
        window_days=int(g("lernen", "fenster_tage", default=28)),
        max_excluded_days=int(g("lernen", "max_ausschluss_tage", default=14)),
        min_days_after_feedback=int(g("lernen", "min_tage_nach_feedback", default=3)),
        prelearn_days=int(g("lernen", "vorlernen_tage", default=35)),
        stable_days=int(g("erkennung", "stabil_tage", default=3)),
        rel_threshold=float(g("erkennung", "rel_schwelle", default=0.25)),
        z_threshold=float(g("erkennung", "z_schwelle", default=3.0)),
        normal_days_to_clear=int(g("erkennung", "normal_tage_bis_entwarnung", default=2)),
        notify_recovery=bool(g("erkennung", "entwarnung_melden", default=True)),
        program_deviation=float(g("erkennung", "programm_abweichung", default=0.3)),
        program_stable_runs=int(g("erkennung", "programm_stabil_laeufe", default=2)),
        stale_after=parse_duration(g("ueberwachung", "keine_daten_nach", default="30m")),
        stale_notify=bool(g("ueberwachung", "keine_daten_melden", default=False)),
        unavailable_after=parse_duration(g("ueberwachung", "nicht_verfuegbar_nach", default="30m")),
        ha_down_after=parse_duration(g("ueberwachung", "ha_weg_nach", default="5m")),
        house_stale_after=parse_duration(g("ueberwachung", "hausdaten_weg_nach", default="30m")),
        poll_interval=parse_duration(g("ueberwachung", "abfrageintervall", default="60s")),
        registry_interval=parse_duration(g("ueberwachung", "registry_pruefung", default="10m")),
        db_path=g("speicher", "pfad", default="/data/stromfresser.sqlite"),
        flush_interval=parse_duration(g("speicher", "schreibintervall", default="60s")),
        raw_days=int(g("speicher", "rohdaten_tage", default=14)),
        minute_days=int(g("speicher", "minutenwerte_tage", default=90)),
        quarter_days=int(g("speicher", "viertelstunden_tage", default=730)),
        report_day=int(g("bericht", "tag", default=1)),
        report_time=(int(rt[0]), int(rt[1])),
        report_dir=g("bericht", "verzeichnis", default="/data/berichte"),
        fridge_reference_kwh=float(g("bericht", "kuehlschrank_referenz_kwh_jahr", default=150)),
        dle_saving_share=float(g("bericht", "durchlauferhitzer_ersparnis_anteil", default=0.3)),
        sensor_prefix=g("home_assistant", "sensor_praefix", default="stromfresser"),
        house=house,
        devices=devices,
        raw=raw,
    )


def _get(d, keys, default):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur or cur[k] is None:
            return default
        cur = cur[k]
    return cur
