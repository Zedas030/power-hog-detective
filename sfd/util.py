"""Small helpers: durations, time zones, robust statistics, formatting."""
from __future__ import annotations

import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_DUR = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(s|sek|m|min|h|std|d|t|tage?)?\s*$", re.I)
_UNIT = {"s": 1, "sek": 1, "m": 60, "min": 60, "h": 3600, "std": 3600, "d": 86400, "t": 86400, "tag": 86400, "tage": 86400}


def parse_duration(value, default_unit: str = "s") -> float | None:
    """'90s', '15m', '3h', '2d', 45 -> seconds. None stays None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) * _UNIT[default_unit]
    m = _DUR.match(str(value))
    if not m:
        raise ValueError(f"Ungültige Dauer: {value!r} (Beispiele: 90s, 15m, 3h, 2d)")
    num = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or default_unit).lower()
    return num * _UNIT[unit]


class Clock:
    """Injectable clock so tests and scenarios can run in simulated time."""

    def __init__(self, tz: str = "Europe/Berlin"):
        self.tz = ZoneInfo(tz)
        self._offset = 0.0
        self._fixed: float | None = None

    def now(self) -> float:
        return self._fixed if self._fixed is not None else time.time() + self._offset

    def set(self, ts: float | None) -> None:
        self._fixed = ts

    def local(self, ts: float | None = None) -> datetime:
        return datetime.fromtimestamp(self.now() if ts is None else ts, self.tz)

    def day_bounds(self, d: date) -> tuple[float, float]:
        start = datetime(d.year, d.month, d.day, tzinfo=self.tz)
        end = start + timedelta(days=1)
        # handle DST properly: re-localise end
        end = datetime(end.year, end.month, end.day, tzinfo=self.tz)
        return start.timestamp(), end.timestamp()

    def day_of(self, ts: float) -> date:
        return self.local(ts).date()

    def at(self, d: date, hour: int, minute: int = 0) -> float:
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=self.tz).timestamp()


def median(values):
    v = sorted(x for x in values if x is not None and not math.isnan(x))
    n = len(v)
    if n == 0:
        return None
    mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2


def quantile(values, q: float):
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    pos = (len(v) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def mad(values, med=None):
    v = [x for x in values if x is not None]
    if not v:
        return None
    med = median(v) if med is None else med
    return median([abs(x - med) for x in v]) * 1.4826


def weighted_median(pairs):
    """pairs: (value, weight). Time-weighted median of a step signal."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def fmt_num(x, digits=0) -> str:
    if x is None:
        return "–"
    s = f"{x:,.{digits}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_minutes(minutes: float) -> str:
    if minutes is None:
        return "–"
    if minutes >= 90:
        return f"{fmt_num(minutes / 60, 1)} Std."
    return f"{fmt_num(minutes, 0)} Min."


def fmt_clock(minute_of_day: float) -> str:
    m = int(round(minute_of_day)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
          "Oktober", "November", "Dezember"]


def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()
