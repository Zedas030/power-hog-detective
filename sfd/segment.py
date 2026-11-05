"""Turn power samples (step signal) into intervals, runs, energy and aggregates."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Run:
    start: float
    end: float
    wh: float
    peak: float
    mean_w: float
    open: bool = False  # still running at end of available data
    program: str | None = None
    flag: str | None = None

    @property
    def minutes(self) -> float:
        return (self.end - self.start) / 60


def steps(samples, start: float, end: float, max_gap: float = 600.0):
    """Sample-and-hold intervals (t0, t1, w) clipped to [start, end).

    A sample is held until the next one, but at most `max_gap` seconds; beyond that the
    signal counts as missing (no interval). `samples` must be sorted by time and may
    contain one sample before `start`.
    """
    out = []
    n = len(samples)
    for i, (ts, w) in enumerate(samples):
        if w is None:
            continue
        nxt = samples[i + 1][0] if i + 1 < n else end
        t1 = min(nxt, ts + max_gap, end)
        t0 = max(ts, start)
        if t1 > t0:
            out.append((t0, t1, float(w)))
    return out


def covered(st) -> float:
    return sum(t1 - t0 for t0, t1, _ in st)


def energy_wh(st) -> float:
    return sum((t1 - t0) * w for t0, t1, w in st) / 3600.0


def runs(st, on_w: float, gap: float, min_run: float, data_end: float | None = None):
    """Find periods with power >= on_w. Off-phases shorter than `gap` are merged into one run."""
    result = []
    cur = None  # [start, end, wh, peak]
    pending = 0.0  # energy of a low-power phase that may still belong to the run
    last_t = None
    for t0, t1, w in st:
        if cur is not None and last_t is not None and t0 - last_t > gap:
            result.append(cur)  # data hole longer than the merge gap closes the run
            cur, pending = None, 0.0
        last_t = t1
        e = (t1 - t0) * w / 3600
        if w >= on_w:
            if cur is not None and t0 - cur[1] <= gap:
                cur[1] = t1
                cur[2] += pending + e
                cur[3] = max(cur[3], w)
            else:
                if cur is not None:
                    result.append(cur)
                cur = [t0, t1, e, w]
            pending = 0.0
        elif cur is not None:
            if t0 - cur[1] > gap:
                result.append(cur)
                cur, pending = None, 0.0
            else:
                pending += e
    if cur is not None:
        result.append(cur)
    out = []
    end_of_data = data_end if data_end is not None else (st[-1][1] if st else None)
    for s, e, wh, peak in result:
        if e - s < min_run:
            continue
        r = Run(start=s, end=e, wh=wh, peak=peak, mean_w=wh * 3600 / max(e - s, 1))
        if end_of_data is not None and end_of_data - e <= gap:
            r.open = True
        out.append(r)
    return out


def time_weighted(st, predicate=None):
    """[(w, seconds)] for intervals matching predicate."""
    return [(w, t1 - t0) for t0, t1, w in st if predicate is None or predicate(w)]


def buckets(st, start: float, end: float, size: float):
    """Average and max per bucket: [(bucket_start, avg_w, max_w, covered_s)] for buckets with data."""
    acc: dict[int, list] = {}
    for t0, t1, w in st:
        t = t0
        while t < t1:
            b = int((t - start) // size)
            b_end = start + (b + 1) * size
            seg_end = min(t1, b_end)
            a = acc.setdefault(b, [0.0, 0.0, w])
            a[0] += (seg_end - t) * w
            a[1] += seg_end - t
            a[2] = max(a[2], w)
            t = seg_end
    out = []
    for b in sorted(acc):
        ws, cov, mx = acc[b]
        if cov > 0:
            out.append((start + b * size, ws / cov, mx, cov))
    return out


def hourly_energy(st, start: float, end: float, min_cover: float = 0.5):
    """Energy per hour [(hour_start, wh)], extrapolated for partly covered hours (>= min_cover)."""
    out = []
    for b, avg, _mx, cov in buckets(st, start, end, 3600):
        if cov >= 3600 * min_cover:
            out.append((b, avg))  # avg W over one hour == Wh
    return out
