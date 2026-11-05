"""SQLite storage with buffered writes and tiered compaction (gentle on SD cards)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (dev TEXT, ts REAL, w REAL, PRIMARY KEY (dev, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS minute (dev TEXT, ts INTEGER, w REAL, wmax REAL, PRIMARY KEY (dev, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS quarter (dev TEXT, ts INTEGER, w REAL, wmax REAL, PRIMARY KEY (dev, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS energy_hourly (dev TEXT, ts INTEGER, wh REAL, src TEXT, PRIMARY KEY (dev, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily (dev TEXT, day TEXT, metric TEXT, value REAL, PRIMARY KEY (dev, day, metric)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS day_flags (dev TEXT, metric TEXT, day TEXT, direction INTEGER, PRIMARY KEY (dev, metric, day)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS runs (dev TEXT, start REAL, end REAL, wh REAL, peak REAL, program TEXT, flag TEXT,
    PRIMARY KEY (dev, start)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS programs (dev TEXT, pid TEXT, name TEXT, recent TEXT, PRIMARY KEY (dev, pid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, dev TEXT, metric TEXT, kind TEXT, direction INTEGER,
    start_day TEXT, opened REAL, state TEXT, message TEXT, value REAL, baseline REAL,
    details TEXT, grp TEXT, notified REAL, closed REAL, test INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS anomalies_open ON anomalies(state, dev, metric);
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, dev TEXT, anomaly_id INTEGER,
    source TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS baseline_start (dev TEXT, metric TEXT, day TEXT, reason TEXT, PRIMARY KEY (dev, metric)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS overrides (dev TEXT, key TEXT, value REAL, PRIMARY KEY (dev, key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS entities (entity_id TEXT PRIMARY KEY, unique_id TEXT, platform TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, title TEXT, message TEXT,
    data TEXT, sent REAL, attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.executescript(SCHEMA)
        self._buf: list[tuple[str, float, float]] = []

    # ---------- generic ----------
    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def many(self, sql, rows):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany(sql, rows)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        self.flush()
        with self.lock:
            self.db.close()

    def size_bytes(self) -> int:
        if self.path == ":memory:":
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                total += os.path.getsize(p)
        return total

    # ---------- meta ----------
    def meta(self, key, default=None):
        r = self.x("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, key, value):
        self.x("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    # ---------- samples ----------
    def add_sample(self, dev: str, ts: float, w: float):
        with self.lock:
            self._buf.append((dev, round(ts, 3), w))

    def buffered(self) -> int:
        return len(self._buf)

    def flush(self):
        with self.lock:
            rows, self._buf = self._buf, []
        if rows:
            self.many("INSERT OR REPLACE INTO samples VALUES (?,?,?)", rows)
        return len(rows)

    def add_samples(self, dev: str, rows):
        self.many("INSERT OR REPLACE INTO samples VALUES (?,?,?)", [(dev, round(t, 3), w) for t, w in rows])

    def samples(self, dev: str, start: float, end: float, with_previous=True):
        self.flush()
        rows = self.x("SELECT ts, w FROM samples WHERE dev=? AND ts>=? AND ts<? ORDER BY ts",
                      (dev, start, end)).fetchall()
        if with_previous:
            prev = self.x("SELECT ts, w FROM samples WHERE dev=? AND ts<? ORDER BY ts DESC LIMIT 1",
                          (dev, start)).fetchone()
            if prev:
                rows.insert(0, prev)
        return rows

    def last_sample(self, dev: str):
        with self.lock:
            buf = [r for r in self._buf if r[0] == dev]
        if buf:
            r = max(buf, key=lambda r: r[1])
            return r[1], r[2]
        return self.x("SELECT ts, w FROM samples WHERE dev=? ORDER BY ts DESC LIMIT 1", (dev,)).fetchone()

    def sample_span(self, dev: str):
        return self.x("SELECT MIN(ts), MAX(ts) FROM samples WHERE dev=?", (dev,)).fetchone()

    # ---------- aggregates ----------
    def put_aggregates(self, table: str, dev: str, rows):
        assert table in ("minute", "quarter")
        self.many(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?)", [(dev, int(t), w, m) for t, w, m in rows])

    def aggregates(self, table: str, dev: str, start: float, end: float):
        assert table in ("minute", "quarter")
        return self.x(f"SELECT ts, w, wmax FROM {table} WHERE dev=? AND ts>=? AND ts<? ORDER BY ts",
                      (dev, start, end)).fetchall()

    def put_energy(self, dev: str, rows, src: str):
        self.many("INSERT OR REPLACE INTO energy_hourly VALUES (?,?,?,?)", [(dev, int(t), wh, src) for t, wh in rows])

    def put_energy_if_missing(self, dev: str, rows, src: str):
        self.many("INSERT OR IGNORE INTO energy_hourly VALUES (?,?,?,?)", [(dev, int(t), wh, src) for t, wh in rows])

    def energy(self, dev: str, start: float, end: float):
        return self.x("SELECT ts, wh FROM energy_hourly WHERE dev=? AND ts>=? AND ts<? ORDER BY ts",
                      (dev, start, end)).fetchall()

    # ---------- daily features ----------
    def put_daily(self, dev: str, day: str, metrics: dict):
        rows = [(dev, day, k, float(v)) for k, v in metrics.items() if v is not None]
        self.x("DELETE FROM daily WHERE dev=? AND day=?", (dev, day))
        if rows:
            self.many("INSERT OR REPLACE INTO daily VALUES (?,?,?,?)", rows)

    def daily(self, dev: str, metric: str, first_day: str, last_day: str):
        return self.x("SELECT day, value FROM daily WHERE dev=? AND metric=? AND day>=? AND day<=? ORDER BY day",
                      (dev, metric, first_day, last_day)).fetchall()

    def daily_day(self, dev: str, day: str) -> dict:
        return dict(self.x("SELECT metric, value FROM daily WHERE dev=? AND day=?", (dev, day)).fetchall())

    def daily_days(self, dev: str) -> list[str]:
        return [r[0] for r in self.x("SELECT DISTINCT day FROM daily WHERE dev=? ORDER BY day", (dev,))]

    def set_flag(self, dev, metric, day, direction):
        if direction:
            self.x("INSERT OR REPLACE INTO day_flags VALUES (?,?,?,?)", (dev, metric, day, direction))
        else:
            self.x("DELETE FROM day_flags WHERE dev=? AND metric=? AND day=?", (dev, metric, day))

    def flags(self, dev, metric, first_day, last_day) -> dict:
        return dict(self.x("SELECT day, direction FROM day_flags WHERE dev=? AND metric=? AND day>=? AND day<=?",
                           (dev, metric, first_day, last_day)).fetchall())

    # ---------- runs & programs ----------
    def put_runs(self, dev: str, runs):
        self.many("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
                  [(dev, r.start, r.end, r.wh, r.peak, r.program, r.flag) for r in runs])

    def runs(self, dev: str, start: float, end: float):
        return self.x("SELECT start, end, wh, peak, program, flag FROM runs WHERE dev=? AND start>=? AND start<? "
                      "ORDER BY start", (dev, start, end)).fetchall()

    def programs(self, dev: str) -> dict:
        out = {}
        for pid, name, recent in self.x("SELECT pid, name, recent FROM programs WHERE dev=? ORDER BY pid", (dev,)):
            out[pid] = {"name": name, "recent": json.loads(recent)}
        return out

    def put_program(self, dev, pid, name, recent):
        self.x("INSERT OR REPLACE INTO programs VALUES (?,?,?,?)", (dev, pid, name, json.dumps(recent)))

    # ---------- anomalies ----------
    def open_anomaly(self, **a) -> int:
        cols = ["dev", "metric", "kind", "direction", "start_day", "opened", "state", "message", "value",
                "baseline", "details", "grp", "test"]
        a.setdefault("state", "offen")
        a.setdefault("test", 0)
        a["details"] = json.dumps(a.get("details") or {}, ensure_ascii=False)
        cur = self.x(f"INSERT INTO anomalies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     tuple(a.get(c) for c in cols))
        return cur.lastrowid

    def anomaly(self, aid: int):
        r = self.x("SELECT * FROM anomalies WHERE id=?", (aid,)).fetchone()
        return self._arow(r) if r else None

    def find_open(self, dev, metric, test=0):
        r = self.x("SELECT * FROM anomalies WHERE dev=? AND metric=? AND state IN ('offen','stumm') AND test=? "
                   "ORDER BY id DESC LIMIT 1", (dev, metric, test)).fetchone()
        return self._arow(r) if r else None

    def anomalies(self, states=("offen",), dev=None, limit=200, test=None):
        q = f"SELECT * FROM anomalies WHERE state IN ({','.join('?' * len(states))})"
        args = list(states)
        if dev:
            q += " AND dev=?"
            args.append(dev)
        if test is not None:
            q += " AND test=?"
            args.append(test)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [self._arow(r) for r in self.x(q, args).fetchall()]

    def recent_anomalies(self, limit=50):
        return [self._arow(r) for r in self.x("SELECT * FROM anomalies ORDER BY id DESC LIMIT ?", (limit,))]

    def update_anomaly(self, aid, **kv):
        if "details" in kv:
            kv["details"] = json.dumps(kv["details"], ensure_ascii=False)
        sets = ",".join(f"{k}=?" for k in kv)
        self.x(f"UPDATE anomalies SET {sets} WHERE id=?", (*kv.values(), aid))

    def _arow(self, r):
        cols = [d[0] for d in self.x("SELECT * FROM anomalies LIMIT 0").description]
        d = dict(zip(cols, r))
        d["details"] = json.loads(d["details"] or "{}")
        return d

    # ---------- feedback / baseline / overrides ----------
    def add_feedback(self, ts, dev, anomaly_id, source, note):
        self.x("INSERT INTO feedback (ts, dev, anomaly_id, source, note) VALUES (?,?,?,?,?)",
               (ts, dev, anomaly_id, source, note))

    def feedback_list(self, limit=50):
        return self.x("SELECT ts, dev, anomaly_id, source, note FROM feedback ORDER BY id DESC LIMIT ?",
                      (limit,)).fetchall()

    def baseline_start(self, dev, metric):
        r = self.x("SELECT day, reason FROM baseline_start WHERE dev=? AND metric=?", (dev, metric)).fetchone()
        return r if r else (None, None)

    def set_baseline_start(self, dev, metric, day, reason):
        self.x("INSERT OR REPLACE INTO baseline_start VALUES (?,?,?,?)", (dev, metric, day, reason))

    def override(self, dev, key, default=None):
        r = self.x("SELECT value FROM overrides WHERE dev=? AND key=?", (dev, key)).fetchone()
        return r[0] if r else default

    def set_override(self, dev, key, value):
        if value is None:
            self.x("DELETE FROM overrides WHERE dev=? AND key=?", (dev, key))
        else:
            self.x("INSERT OR REPLACE INTO overrides VALUES (?,?,?)", (dev, key, value))

    # ---------- entities ----------
    def entity_unique(self, entity_id):
        r = self.x("SELECT unique_id, platform FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        return r

    def set_entity(self, entity_id, unique_id, platform):
        self.x("INSERT OR REPLACE INTO entities VALUES (?,?,?)", (entity_id, unique_id, platform))

    # ---------- outbox ----------
    def enqueue(self, ts, title, message, data=None) -> int:
        cur = self.x("INSERT INTO outbox (ts, title, message, data) VALUES (?,?,?,?)",
                     (ts, title, message, json.dumps(data or {}, ensure_ascii=False)))
        return cur.lastrowid

    def pending(self):
        return [(i, t, ti, m, json.loads(d or "{}"), a) for i, t, ti, m, d, a in
                self.x("SELECT id, ts, title, message, data, attempts FROM outbox WHERE sent IS NULL ORDER BY id")]

    def mark_sent(self, oid, ts):
        self.x("UPDATE outbox SET sent=? WHERE id=?", (ts, oid))

    def mark_attempt(self, oid):
        self.x("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (oid,))

    def sent_messages(self, limit=50):
        return self.x("SELECT id, ts, title, message, sent FROM outbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---------- compaction ----------
    def compact(self, now: float, raw_days: int, minute_days: int, quarter_days: int, outbox_days: int = 90):
        """Delete data that has been condensed into the next tier. Returns rows removed per table."""
        self.flush()
        removed = {}
        for table, days in (("samples", raw_days), ("minute", minute_days), ("quarter", quarter_days)):
            cur = self.x(f"DELETE FROM {table} WHERE ts < ?", (now - days * 86400,))
            removed[table] = cur.rowcount
        cur = self.x("DELETE FROM outbox WHERE sent IS NOT NULL AND sent < ?", (now - outbox_days * 86400,))
        removed["outbox"] = cur.rowcount
        self.x("PRAGMA incremental_vacuum(2000)")
        self.x("PRAGMA wal_checkpoint(TRUNCATE)")
        return removed
