"""Language selection (English default, German) and number/date formatting."""
from __future__ import annotations

LANGS = ("en", "de")
_lang = "en"


def set_lang(lang: str | None) -> None:
    global _lang
    _lang = lang if lang in LANGS else "en"


def lang() -> str:
    return _lang


def L(en: str, de: str) -> str:
    """Pick the text for the active language."""
    return de if _lang == "de" else en


WEEKDAYS = {"en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
            "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]}
MONTHS = {"en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December"],
          "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
                 "November", "Dezember"]}


def weekday(i: int) -> str:
    return WEEKDAYS[_lang][i]


def month(i: int) -> str:
    return MONTHS[_lang][i]


def fmt_num(x, digits=0) -> str:
    if x is None:
        return "–"
    s = f"{x:,.{digits}f}"
    if _lang == "de":
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def fmt_minutes(minutes: float) -> str:
    if minutes is None:
        return "–"
    if minutes >= 90:
        return f"{fmt_num(minutes / 60, 1)} " + L("h", "Std.")
    return f"{fmt_num(minutes, 0)} " + L("min", "Min.")


def fmt_date(d) -> str:
    return d.strftime("%d.%m.") if _lang == "de" else d.strftime("%b %d")


def fmt_datetime(dt) -> str:
    return dt.strftime("%d.%m.%Y %H:%M") if _lang == "de" else dt.strftime("%Y-%m-%d %H:%M")
