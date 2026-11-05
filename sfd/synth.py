"""Synthetic consumption data for tests and acceptance scenarios."""
from __future__ import annotations

import random


def sample(power_fn, start: float, end: float, step: float = 15.0):
    out = []
    t = start
    while t < end:
        out.append((t, round(power_fn(t), 2)))
        t += step
    return out


def fridge(start: float, end: float, cycle_min: float = 20.0, pause_min: float = 40.0, on_w: float = 85.0,
           standby_w: float = 1.5, jitter: float = 0.08, seed: int = 1, step: float = 15.0):
    """Compressor cycles: on for cycle_min, off for pause_min (with random jitter)."""
    rnd = random.Random(seed)
    edges = []  # (on_start, on_end)
    t = start - rnd.uniform(0, (cycle_min + pause_min) * 60)
    while t < end:
        on = cycle_min * 60 * (1 + rnd.uniform(-jitter, jitter))
        off = pause_min * 60 * (1 + rnd.uniform(-jitter, jitter))
        edges.append((t, t + on))
        t += on + off
    idx = [0]

    def p(ts):
        while idx[0] < len(edges) - 1 and edges[idx[0]][1] <= ts:
            idx[0] += 1
        a, b = edges[idx[0]]
        if a <= ts < b:
            return on_w * (1.4 if ts - a < 30 else 1.0) + rnd.uniform(-2, 2)
        return standby_w

    return sample(p, start, end, step)


def constant(start: float, end: float, w: float, noise: float = 0.5, seed: int = 2, step: float = 15.0):
    rnd = random.Random(seed)
    return sample(lambda t: max(0.0, w + rnd.uniform(-noise, noise)), start, end, step)


def schedule(start: float, end: float, day_start_fn, on_min: float, off_min: float, on_w: float,
             off_w: float = 0.5, seed: int = 3, step: float = 15.0):
    """Daily on/off schedule (minutes after local midnight). day_start_fn(ts) -> local midnight ts."""
    rnd = random.Random(seed)

    def p(ts):
        m = (ts - day_start_fn(ts)) / 60
        on = on_min <= m < off_min if on_min < off_min else (m >= on_min or m < off_min)
        return on_w + rnd.uniform(-1, 1) if on else off_w

    return sample(p, start, end, step)


def runs(start: float, end: float, windows, on_w: float, off_w: float = 0.3, seed: int = 4, step: float = 15.0):
    """Arbitrary on-windows [(t_on, t_off, watts?)]: e.g. an appliance left running."""
    rnd = random.Random(seed)
    ws = sorted(windows)

    def p(ts):
        for w in ws:
            a, b = w[0], w[1]
            if a <= ts < b:
                return (w[2] if len(w) > 2 else on_w) + rnd.uniform(-1, 1)
        return off_w

    return sample(p, start, end, step)


def dishwasher(start: float, end: float, day_start_fn, at_min: float = 19 * 60, minutes: float = 120,
               kwh: float = 1.0, every_days: int = 1, seed: int = 5, step: float = 15.0):
    """Program run: two heating phases with a low-power pump phase in between."""
    rnd = random.Random(seed)
    heat_s = 0.25 * minutes * 60
    heat_w = (kwh * 1000 * 3600 * 0.9) / (2 * heat_s)
    pump_w = 25.0

    def p(ts):
        d0 = day_start_fn(ts)
        if int((d0 - day_start_fn(start)) / 86400 + 0.5) % every_days:
            return 0.4
        m = (ts - d0) / 60 - at_min
        if m < 0 or m >= minutes:
            return 0.4
        frac = m / minutes
        if frac < 0.25 or 0.6 <= frac < 0.85:
            return heat_w + rnd.uniform(-20, 20)
        return pump_w + rnd.uniform(-3, 3)

    return sample(p, start, end, step)
