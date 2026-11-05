"""Robust baseline (median + MAD) and the deviation test for one daily metric."""
from __future__ import annotations

from dataclasses import dataclass

from .util import mad, median


@dataclass
class Result:
    status: str  # 'lernt' | 'ok' | 'abweichung'
    n: int
    baseline: float | None = None
    sigma: float | None = None
    value: float | None = None
    rel: float | None = None
    z: float | None = None
    direction: int = 0

    @property
    def deviating(self) -> bool:
        return self.status == "abweichung"


def circ(d: float) -> float:
    return ((d + 720.0) % 1440.0) - 720.0


def evaluate(history: list[float], x: float, *, min_abs: float, rel_threshold: float | None,
             use_rel: bool, circular: bool, z_threshold: float, min_n: int) -> Result:
    n = len(history)
    if n < min_n:
        return Result("lernt", n, value=x)
    vals = list(history)
    if circular:
        ref = vals[-1]
        vals = [ref + circ(v - ref) for v in vals]
        med0 = median(vals)
        x = med0 + circ(x - med0)
    med = median(vals)
    s = mad(vals, med) or 0.0
    sigma = max(s, min_abs / 3.0, abs(med) * 0.03 if not circular else 0.0, 1e-9)
    diff = x - med
    rel = diff / abs(med) if (med and not circular) else None
    z = diff / sigma
    dev = abs(diff) >= min_abs and abs(z) >= z_threshold
    if dev and use_rel and rel is not None and rel_threshold is not None:
        dev = abs(rel) >= rel_threshold
    return Result("abweichung" if dev else "ok", n, baseline=med % 1440 if circular else med, sigma=sigma,
                  value=x % 1440 if circular else x, rel=rel, z=z, direction=(1 if diff > 0 else -1) if dev else 0)
