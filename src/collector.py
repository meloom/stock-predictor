"""collector.py — S1 as an always-on, queue-driven data collector.

Instead of one batch pass, S1 is a persistent QUEUE of collection tasks that a
worker drains continuously, strictly pacing each SOURCE to its rate limit so we
keep consuming quota without ever exceeding it. Everything is written to the
FeatureStore; every other stage just reads from it.

Model (all state in the same SQLite DB as the store):
  - a task = (source, kind, scope). `kind` is a data type (bars, macro, short,
    analyst, implied_move, ...); `scope` is a ticker or the market sentinel.
  - RECURRING tasks (interval_sec set) keep recent data fresh; they reschedule
    themselves. BACKFILL = enqueue a task due-now with a boosted priority (what
    `add_ticker` / `add_source` / `enqueue_backfill` do), so new tickers/sources
    jump the line.
  - a persistent per-source RATE LIMITER (rolling call ledger) gates execution:
    a task runs only if its source has enough quota for the call(s) it needs, so
    multiple processes / cron runs still obey the limit.

Extensible: `register_source` (name + limit), `register_kind` (cadence + priority
+ handler). Add news, options greeks, a new vendor, etc. by registering a handler.

Run modes:
  python -m collector seed                 # ensure recurring tasks for the universe
  python -m collector add-ticker NVDA      # enqueue a prioritized backfill
  python -m collector drain --seconds 55   # do one bounded pass (per-minute cron)
  python -m collector run                  # long-running daemon
  python -m collector status               # queue + quota snapshot
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import FeatureStore, DEFAULT_DB, MARKET_SCOPE   # noqa: E402
from universe import UNIVERSE                             # noqa: E402

DAY = 86400
BACKFILL_PRIORITY_BOOST = 1000   # backfill tasks run ahead of every routine refresh
# Fixed history horizon: collect every backfillable signal from this date until now,
# then keep accruing forward. History-mode fetch ranges and coverage expectations are
# measured against this start, NOT a rolling window.
COLLECTION_START = date(2025, 7, 1)
# Native signal FREQUENCY taxonomy. Fixed cadences (5min..weekly) are regular time
# series measured by depth; 'event' is irregular/undefined (collect all new from the
# last watermark to now); 'snapshot' is point-in-time state that accrues forward.
FREQ_SECONDS = {"5min": 300, "hourly": 3600, "daily": DAY, "weekly": 7 * DAY}
FREQ_PPD = {"5min": 78, "hourly": 7, "daily": 1, "weekly": 0.2}   # points per TRADING day
FREQ_MODE = {"5min": "history", "hourly": "history", "daily": "history",
             "weekly": "history", "event": "rolling", "snapshot": "snapshot"}
# freshness SLA per frequency: (green_max_sec, yellow_max_sec); beyond yellow = red.
DEFAULT_SLA = {"5min": (300, 1800), "hourly": (3600, 10800), "daily": (DAY, 7 * DAY),
               "weekly": (7 * DAY, 14 * DAY), "event": (3 * DAY, 7 * DAY),
               "snapshot": (6 * 3600, DAY)}
CREDENTIALS = Path.home() / ".credentials"
# S1 kind -> its typed table (schema.py) for honest coverage/depth measurement.
KIND_TABLE = {
    "bars": "bars", "quote": "quotes", "macro": "macro", "short": "short_interest",
    "implied_move": "options_implied", "earn_report": "earnings_reports",
    "earn_date": "earnings_calendar", "insider": "insider_transactions",
    "analyst_revisions": "analyst_revisions", "statements": "fundamentals",
    "sec_filings": "sec_filings", "xbrl": "xbrl_financials", "transcript": "transcripts",
}

_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_tasks (
    task_id      TEXT PRIMARY KEY,      -- "{source}:{kind}:{scope}"
    source       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    scope        TEXT NOT NULL,
    priority     INTEGER NOT NULL,      -- lower runs first
    interval_sec INTEGER,               -- recurring cadence; NULL = one-shot
    next_due     TEXT NOT NULL,         -- ISO timestamp
    status       TEXT NOT NULL,         -- 'pending' | 'done' | 'disabled'
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_ok      TEXT,
    last_error   TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_due ON collection_tasks (status, priority, next_due);
CREATE TABLE IF NOT EXISTS source_calls (   -- rolling rate-limit ledger
    source TEXT NOT NULL,
    ts     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls ON source_calls (source, ts);
"""


def load_secret(name: str) -> str | None:
    """Read KEY=value from ~/.credentials (never committed)."""
    import os
    if os.environ.get(name):
        return os.environ[name]
    if not CREDENTIALS.exists():
        return None
    for line in CREDENTIALS.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return None


class Collector:
    def __init__(self, db_path=DEFAULT_DB, store: FeatureStore | None = None, now_fn=None):
        import sqlite3
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(self.db_path)
        self.c.execute("PRAGMA busy_timeout=5000")
        self.c.executescript(_QUEUE_SCHEMA)
        self.store = store or FeatureStore(db_path)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.limits: dict[str, tuple[int, int]] = {}     # source -> (limit, window_sec)
        self.kinds: dict[str, dict] = {}                 # kind -> spec
        self.handlers: dict[tuple, callable] = {}        # (source, kind) -> fn

    def _now(self):
        return self._now_fn()

    # ── registration ────────────────────────────────────────────────────────
    def register_source(self, source: str, limit: int, window_sec: int):
        self.limits[source] = (limit, window_sec)

    def register_kind(self, kind, source, priority, handler, frequency="snapshot",
                      interval_sec=None, scope="ticker", est_calls=1, stage="S1",
                      fresh_sla=None):
        """Declare a data kind by its native FREQUENCY:
          5min|hourly|daily|weekly — fixed-cadence time series. Coverage = fraction of
              the expected points in [COLLECTION_START, now] at that cadence that are
              actually stored. Poll interval defaults to the cadence.
          event  — irregular / undefined (insider, revisions, earnings, short prints):
              collect ALL new records from the last-collected watermark to now; coverage
              = tickers covered + record count + span, not a fixed depth.
          snapshot — point-in-time state (quote, analyst consensus, implied move, next-
              earnings date): re-sampled each poll; history accrues forward.
        `interval_sec` overrides the default poll cadence (e.g. quote polled every 5 min
        for freshness). `fresh_sla=(green_sec, yellow_sec)` overrides the freshness
        thresholds. Declaring a kind is all it takes — reconcile() auto-backfills it."""
        interval = interval_sec if interval_sec is not None else FREQ_SECONDS.get(frequency, DAY)
        self.kinds[kind] = {"source": source, "interval": interval, "priority": priority,
                            "scope": scope, "est_calls": est_calls, "stage": stage,
                            "frequency": frequency, "mode": FREQ_MODE.get(frequency, "snapshot"),
                            "sla": fresh_sla or DEFAULT_SLA.get(frequency, DEFAULT_SLA["snapshot"])}
        self.handlers[(source, kind)] = handler

    # ── rate limiter (persistent, rolling window) ─────────────────────────────
    def available(self, source: str) -> int:
        limit, window = self.limits.get(source, (10 ** 9, 1))
        cutoff = (self._now() - timedelta(seconds=window)).isoformat()
        n = self.c.execute("SELECT COUNT(*) FROM source_calls WHERE source=? AND ts>?",
                           (source, cutoff)).fetchone()[0]
        return max(0, limit - n)

    def _record_calls(self, source: str, n: int):
        ts = self._now().isoformat()
        self.c.executemany("INSERT INTO source_calls VALUES (?,?)", [(source, ts)] * max(1, n))
        # prune anything older than the largest window we track
        oldest = (self._now() - timedelta(seconds=max((w for _, w in self.limits.values()), default=1) + 5)).isoformat()
        self.c.execute("DELETE FROM source_calls WHERE ts<?", (oldest,))
        self.c.commit()

    # ── enqueue ───────────────────────────────────────────────────────────────
    def _upsert(self, source, kind, scope, priority, interval, next_due, force_due=False):
        tid = f"{source}:{kind}:{scope}"
        now = self._now().isoformat()
        existing = self.c.execute("SELECT next_due FROM collection_tasks WHERE task_id=?", (tid,)).fetchone()
        if existing is None:
            self.c.execute(
                "INSERT INTO collection_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, source, kind, scope, priority, interval, next_due, "pending", 0, None, None, now))
        elif force_due:
            self.c.execute("UPDATE collection_tasks SET status='pending', priority=?, "
                           "next_due=?, updated_at=? WHERE task_id=?",
                           (priority, next_due, now, tid))
        self.c.commit()
        return tid

    def enqueue_backfill(self, kind, scope, priority_boost=100):
        """Prioritized one-shot-ish enqueue: make (kind, scope) due NOW with a
        boosted priority so a newly added ticker/source is collected first."""
        k = self.kinds[kind]
        self._upsert(k["source"], kind, scope, k["priority"] - priority_boost,
                     k["interval"], self._now().isoformat(), force_due=True)

    def seed(self, tickers: list[str]):
        """Ensure recurring tasks exist for the universe (idempotent). New tasks
        start due-now so a fresh DB backfills immediately."""
        now = self._now().isoformat()
        for kind, k in self.kinds.items():
            if k["scope"] == "market":
                self._upsert(k["source"], kind, MARKET_SCOPE, k["priority"], k["interval"], now)
            else:
                for t in tickers:
                    self._upsert(k["source"], kind, t, k["priority"], k["interval"], now)

    def add_ticker(self, ticker: str):
        """Register a new ticker: enqueue every ticker-scoped kind as a prioritized
        backfill so it's collected ahead of routine refreshes."""
        for kind, k in self.kinds.items():
            if k["scope"] != "market":
                self.enqueue_backfill(kind, ticker)

    def reconcile(self, tickers: list[str] | None = None) -> int:
        """Declarative self-healing (replaces manual seed): ensure every declared
        (kind, scope) is actually COVERED. A (kind, scope) is re-armed as a
        PRIORITIZED BACKFILL (priority boosted below every routine refresh) when it
        is:
          • brand-new (no task yet), OR
          • never successfully collected (null last_ok, not already queued), OR
          • missing from the TYPED store — a task the legacy feature_values
            projection marked 'collected' but which has no typed row is NOT covered,
            so we re-backfill it until a real typed row exists.
        Coverage is judged against the typed tables (schema.py), never the queue's
        presence flag. Idempotent: the daemon calls this on startup and every 5 min,
        so declaring a new kind — or a half-migrated one — heals to real coverage
        with zero manual steps. Returns how many it (re)armed."""
        tickers = tickers or list(UNIVERSE)
        now = self._now().isoformat()
        armed = 0
        # entities actually present in the typed store, per kind (real coverage).
        # Only built for kinds that HAVE a typed table, so tests with plain kinds
        # never touch the typed store.
        present = {}
        typed_kinds = [kd for kd in self.kinds if kd in KIND_TABLE]
        if typed_kinds:
            tc = _typed().c
            for kd in typed_kinds:
                tbl = KIND_TABLE[kd]
                ent = "name" if tbl == "macro" else "ticker"
                try:
                    present[kd] = {r[0] for r in tc.execute(f"SELECT DISTINCT {ent} FROM {tbl}")}
                except Exception:
                    present[kd] = set()
        for kind, k in self.kinds.items():
            scopes = [MARKET_SCOPE] if k["scope"] == "market" else tickers
            bf_prio = k["priority"] - BACKFILL_PRIORITY_BOOST     # runs before all refreshes
            pres = present.get(kind)                              # None if no typed table
            for sc in scopes:
                tid = f"{k['source']}:{kind}:{sc}"
                row = self.c.execute("SELECT last_ok, status, next_due FROM collection_tasks "
                                     "WHERE task_id=?", (tid,)).fetchone()
                # missing from the typed store (ticker scope only; market handled by depth)
                missing_typed = (pres is not None and k["scope"] != "market" and sc not in pres)
                due_now = row is not None and row[2] is not None and row[2] <= now
                if row is None:                                  # brand-new signal/scope
                    self._upsert(k["source"], kind, sc, bf_prio, k["interval"], now); armed += 1
                elif row[0] is None or missing_typed:
                    # never collected, OR marked 'collected' by the legacy projection
                    # but with no typed row -> (re)arm as prioritized backfill and pull
                    # it DUE NOW, even if it is 'pending' but scheduled in the future
                    # (which is exactly why the typed backfill was never running).
                    if row[1] != "pending" or not due_now:
                        self.c.execute("UPDATE collection_tasks SET status='pending', priority=?, "
                                       "next_due=?, last_error=NULL, updated_at=? WHERE task_id=?",
                                       (bf_prio, now, now, tid)); armed += 1
        self.c.commit()
        return armed

    # ── worker ────────────────────────────────────────────────────────────────
    def tick(self):
        """Run at most one due task whose source has quota. Returns a summary
        dict, or None if nothing is runnable right now."""
        now = self._now()
        # ALL due tasks are candidates (no LIMIT): a rate-limited source at the top
        # of the priority order (e.g. 109 polygon tasks) must not starve the fast
        # sources below it — when its quota is spent we fall through to the next
        # source that still has quota.
        rows = self.c.execute(
            "SELECT task_id, source, kind, scope, interval_sec, attempts FROM collection_tasks "
            "WHERE status='pending' AND next_due<=? ORDER BY priority ASC, next_due ASC",
            (now.isoformat(),)).fetchall()
        for task_id, source, kind, scope, interval, attempts in rows:
            est = self.kinds.get(kind, {}).get("est_calls", 1)
            if self.available(source) < est:
                continue                                   # strict: don't start without quota
            handler = self.handlers.get((source, kind))
            if handler is None:
                self._set(task_id, status="disabled", last_error="no handler"); continue
            tid = f"collect:{task_id}:{now.timestamp():.0f}"
            try:
                n_rows, n_calls = handler(scope, self.store, tid)
                self._record_calls(source, n_calls)
                # on success, restore the kind's BASE priority (a backfill task
                # drops back to routine-refresh priority once it has data).
                base_prio = self.kinds.get(kind, {}).get("priority")
                self._reschedule(task_id, interval, now, n_rows, base_prio)
                return {"task": task_id, "rows": n_rows, "calls": n_calls}
            except Exception as e:               # noqa: BLE001 — a failed call still cost quota
                self._record_calls(source, 1)
                self._backoff(task_id, attempts, now, f"{type(e).__name__}: {e}")
                return {"task": task_id, "error": str(e)}
        return None

    def drain(self, seconds: float = 55, sleep: float = 2.0):
        """Do a bounded pass — for a per-minute cron. Runs tasks until `seconds`
        elapse, sleeping briefly when nothing is runnable (quota/nothing due)."""
        start = self._now(); done = 0; errors = 0
        while (self._now() - start).total_seconds() < seconds:
            r = self.tick()
            if r is None:
                time.sleep(sleep)
            elif "error" in r:
                errors += 1
            else:
                done += 1
        return {"ran": done, "errors": errors}

    def run_forever(self, sleep: float = 2.0, reconcile_every: float = 300):
        """Daemon loop. Reconciles on startup and every `reconcile_every` seconds so
        newly-declared kinds auto-backfill with no manual seed."""
        try:
            self.reconcile()
        except Exception:                                    # noqa: BLE001
            pass
        last = self._now()
        while True:
            if (self._now() - last).total_seconds() >= reconcile_every:
                try:
                    self.reconcile()
                except Exception:                            # noqa: BLE001
                    pass
                last = self._now()
            if self.tick() is None:
                time.sleep(sleep)

    # ── task state transitions ────────────────────────────────────────────────
    def _reschedule(self, task_id, interval, now, n_rows, base_priority=None):
        nowi = now.isoformat()
        prio = "" if base_priority is None else ", priority=%d" % base_priority
        if interval:
            nxt = (now + timedelta(seconds=interval)).isoformat()
            self.c.execute(f"UPDATE collection_tasks SET status='pending', next_due=?, attempts=0, "
                           f"last_ok=?, last_error=NULL, updated_at=?{prio} WHERE task_id=?",
                           (nxt, nowi, nowi, task_id))
        else:
            self.c.execute(f"UPDATE collection_tasks SET status='done', attempts=0, last_ok=?, "
                           f"updated_at=?{prio} WHERE task_id=?", (nowi, nowi, task_id))
        self.c.commit()

    def _backoff(self, task_id, attempts, now, err):
        delay = min(6 * 3600, 60 * (2 ** min(attempts, 8)))     # 1m,2m,4m... cap 6h
        nxt = (now + timedelta(seconds=delay)).isoformat()
        self.c.execute("UPDATE collection_tasks SET attempts=?, next_due=?, last_error=?, "
                       "updated_at=? WHERE task_id=?",
                       (attempts + 1, nxt, err, now.isoformat(), task_id))
        self.c.commit()

    def _set(self, task_id, **kw):
        kw["updated_at"] = self._now().isoformat()
        cols = ", ".join(f"{k}=?" for k in kw)
        self.c.execute(f"UPDATE collection_tasks SET {cols} WHERE task_id=?",
                       (*kw.values(), task_id))
        self.c.commit()

    def status(self) -> dict:
        by_status = dict(self.c.execute(
            "SELECT status, COUNT(*) FROM collection_tasks GROUP BY status").fetchall())
        due = self.c.execute("SELECT COUNT(*) FROM collection_tasks WHERE status='pending' AND next_due<=?",
                             (self._now().isoformat(),)).fetchone()[0]
        quota = {s: f"{self.available(s)}/{lim} per {win}s" for s, (lim, win) in self.limits.items()}
        errs = self.c.execute("SELECT task_id, last_error FROM collection_tasks "
                              "WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 5").fetchall()
        return {"by_status": by_status, "due_now": due, "quota": quota, "recent_errors": errs}

    def signal_detail(self, scope: str, feature: str, day_limit: int = 90, ts_limit: int = 300) -> dict:
        """Drill-down for one ticker × signal: daily density (points per event
        date) + the exact collection timestamps (event_time & ingested_at)."""
        daily = [{"date": d, "count": n} for d, n in self.c.execute(
            "SELECT substr(event_time,1,10) d, COUNT(*) FROM feature_values "
            "WHERE feature=? AND scope=? GROUP BY d ORDER BY d DESC LIMIT ?",
            (feature, scope, day_limit)).fetchall()]
        # COLLECTION density: ingested_at bucketed to 5-minute intervals (as fine
        # as we store — shows exactly when/how often this signal was collected).
        buckets = {}
        for m, n in self.c.execute(
                "SELECT substr(ingested_at,1,16) m, COUNT(*) FROM feature_values "
                "WHERE feature=? AND scope=? GROUP BY m ORDER BY m DESC LIMIT 600",
                (feature, scope)).fetchall():
            try:
                base = m[:14] + "%02d" % ((int(m[14:16]) // 5) * 5)   # floor minute to /5
            except Exception:
                base = m
            buckets[base] = buckets.get(base, 0) + n
        by_5min = [{"t": k.replace("T", " "), "count": v} for k, v in sorted(buckets.items())]
        stamps = [{"event_time": et, "ingested_at": ing} for et, ing in self.c.execute(
            "SELECT event_time, ingested_at FROM feature_values WHERE feature=? AND scope=? "
            "ORDER BY ingested_at DESC LIMIT ?", (feature, scope, ts_limit)).fetchall()]
        total = self.c.execute("SELECT COUNT(*) FROM feature_values WHERE feature=? AND scope=?",
                               (feature, scope)).fetchone()[0]
        span = self.c.execute("SELECT MIN(event_time), MAX(event_time) FROM feature_values "
                              "WHERE feature=? AND scope=?", (feature, scope)).fetchone()
        return {"scope": scope, "feature": feature, "total": total,
                "first_event": span[0], "last_event": span[1],
                "daily": list(reversed(daily)), "by_5min": by_5min, "stamps": stamps}

    def typed_tables(self) -> list:
        """The typed raw-data tables + their coverage (for the inspector dropdown)."""
        import schema
        ts = _typed()
        return [ts.coverage(t) for t in schema.SCHEMA]

    def typed_rows(self, table: str, ticker: str | None = None, limit: int = 100) -> dict:
        """Typed rows for one table (real columns incl. the source-timestamp column)."""
        import schema
        if table not in schema.SCHEMA:
            return {"table": table, "columns": [], "rows": [], "error": "unknown table"}
        return _typed().rows(table, ticker or None, limit)

    def raw_values(self, scope: str, feature: str, limit: int = 60) -> dict:
        """The EXACT stored values for one scope × feature (most recent first),
        with event_time (when generated) and ingested_at (when collected). JSON
        values are decoded so the inspector shows real content."""
        import json as J
        rows = []
        for et, ing, val in self.c.execute(
                "SELECT event_time, ingested_at, value FROM feature_values "
                "WHERE feature=? AND scope=? ORDER BY event_time DESC, ingested_at DESC LIMIT ?",
                (feature, scope, limit)).fetchall():
            try:
                v = J.loads(val)
            except Exception:
                v = val
            rows.append({"event_time": et, "ingested_at": ing, "value": v})
        return {"scope": scope, "feature": feature, "count": len(rows), "rows": rows}

    # ── coverage / progress report ────────────────────────────────────────────
    def coverage_report(self, matrix_features: list[str] | None = None) -> dict:
        """A snapshot of collection progress: per-kind backfill %, per-signal
        freshness, and a ticker×signal freshness matrix. Reads BOTH the queue and
        the feature_values table (same DB)."""
        now = self._now()
        nowi = now.isoformat()

        def hours_since(ts):
            if not ts:
                return None
            try:
                return round((now - datetime.fromisoformat(ts)).total_seconds() / 3600, 1)
            except Exception:
                return None

        # per-kind queue aggregates (breadth / freshness / errors). "collected" =
        # succeeded (has last_ok AND no standing error) — an errored task does NOT
        # count. This is a PRESENCE signal; depth is measured separately below.
        agg = {}
        for kind, total, collected, due, errs, last in self.c.execute(
                "SELECT kind, COUNT(*), SUM(last_ok IS NOT NULL AND last_error IS NULL), "
                "SUM(status='pending' AND next_due<=?), SUM(last_error IS NOT NULL), MAX(last_ok) "
                "FROM collection_tasks GROUP BY kind", (nowi,)):
            agg[kind] = {"total": total, "collected": collected or 0, "due": due or 0,
                         "errors": errs or 0, "last": last}

        # HONEST coverage vs EXPECTATION — measured per the kind's MODE, so a snapshot
        # collected once is NOT reported the same as a fully backfilled daily series.
        ts = _typed()
        kinds = []
        for kind, k in self.kinds.items():
            if k.get("stage", "S1") != "S1":
                continue                                  # derived (S2+) — not collection
            a = agg.get(kind, {"total": 0, "collected": 0, "due": 0, "errors": 0, "last": None})
            mode, table = k.get("mode", "snapshot"), KIND_TABLE.get(kind)
            freq = k.get("frequency", "snapshot")
            # expected points = trading days since COLLECTION_START × points-per-day at
            # the signal's native cadence (daily→1, hourly→7, 5min→78, weekly→0.2).
            trading_days = max(1, round((now.date() - COLLECTION_START).days * 5 / 7))
            want = max(1, round(trading_days * FREQ_PPD.get(freq, 1)))
            breadth = round(100 * a["collected"] / a["total"]) if a["total"] else 0
            e = {"kind": kind, "source": k.get("source", "?"), "mode": mode,
                 "frequency": freq, "sla": k.get("sla"),
                 "total": a["total"], "collected": a["collected"], "due_now": a["due"],
                 "errors": a["errors"], "last_run_h": hours_since(a["last"])}
            # coverage is measured from the REAL typed store (schema.py), never the
            # queue's presence count — a task marked "collected" via the legacy
            # projection but with no typed row is NOT coverage.
            exp_ents = 1 if k["scope"] == "market" else max(a["total"], 1)
            if mode == "history" and table:
                d = ts.depth(table, cap=want)
                denom = exp_ents * want
                e.update(pct=min(100, round(100 * d["capped_sum"] / denom)) if denom else 0,
                         detail=f'{d["entities"]}/{exp_ents} tickers · {d["median"]}/{want} {freq} bars deep',
                         expect=f'~{want} {freq} points since {COLLECTION_START} (backfillable)')
            elif mode == "rolling" and table:
                cov = ts.coverage(table)
                span = f'{(cov["first"] or "?")[:10]}→{(cov["last"] or "?")[:10]}'
                avg = round(cov["rows"] / cov["entities"], 1) if cov["entities"] else 0
                e.update(pct=round(100 * cov["entities"] / exp_ents) if exp_ents else 0,
                         detail=f'{cov["entities"]}/{exp_ents} tickers · {cov["rows"]} recs (~{avg}/ticker) · {span}',
                         expect='event history — collect all new from last watermark to now')
            elif table:                                    # snapshot WITH a typed table
                cov = ts.coverage(table)
                e.update(pct=round(100 * cov["entities"] / exp_ents) if exp_ents else 0,
                         detail=f'{cov["entities"]}/{exp_ents} tickers · snapshot, history accrues forward',
                         expect='current snapshot only — no back-history from this source')
            else:                                          # snapshot, no typed table (analyst)
                e.update(pct=breadth,
                         detail=f'{a["collected"]}/{a["total"]} tickers · snapshot (feature_values)',
                         expect='current snapshot only — no back-history from this source')
            kinds.append(e)
        # STABLE order (by collection priority, then name) — never reorder by progress.
        kinds.sort(key=lambda e: (self.kinds.get(e["kind"], {}).get("priority", 999), e["kind"]))

        # per-signal store coverage — only S1 RAW-collected features (derived S2+
        # signals belong to their own stage's dashboard).
        non_s1 = ("tech.", "xsec.", "regime.", "alpha.", "earnings.analysis",
                  "calendar.days_to_earnings", "fundamental.earnings_signal")
        signals = []
        for feature, nrows, nscopes, latest_evt, latest_ing in self.c.execute(
                "SELECT feature, COUNT(*), COUNT(DISTINCT scope), MAX(event_time), MAX(ingested_at) "
                "FROM feature_values GROUP BY feature ORDER BY feature"):
            if any(feature.startswith(p) for p in non_s1):
                continue
            signals.append({"feature": feature, "rows": nrows, "scopes": nscopes,
                            "latest_event": latest_evt, "fresh_h": hours_since(latest_ing)})

        # ticker × signal freshness matrix — read from the TYPED store (real data),
        # NOT the feature_values projection. An uncollected ticker then shows
        # 'missing' instead of a false green from a projection-era row.
        import schema
        MATRIX = [("Bars", "bars", "bars"), ("Quote", "quotes", "quote"),
                  ("Short%", "short_interest", "short"), ("ImplMove", "options_implied", "implied_move"),
                  ("Fundmls", "fundamentals", "statements"), ("NextEarn", "earnings_calendar", "earn_date"),
                  ("Filings", "sec_filings", "sec_filings"), ("Analyst", None, "analyst")]
        def _sla(kd):                                     # freshness thresholds for a column's kind
            return self.kinds.get(kd, {}).get("sla", DEFAULT_SLA["snapshot"])
        cols = [{"label": label, "sla": _sla(kd)} for label, _, kd in MATRIX]
        matrix = {}
        for label, table, kd in MATRIX:
            if table:
                tcol = schema.SCHEMA[table][0]
                try:
                    rows_ = ts.c.execute(
                        f"SELECT ticker, MAX(ingested_at), COUNT(*), MIN({tcol}), MAX({tcol}) "
                        f"FROM {table} GROUP BY ticker").fetchall()
                except Exception:
                    rows_ = []
            else:                                     # analyst snapshot lives in feature_values
                rows_ = self.c.execute(
                    "SELECT scope, MAX(ingested_at), COUNT(*), MIN(event_time), MAX(event_time) "
                    "FROM feature_values WHERE feature='fundamental.analyst_snapshot' "
                    "GROUP BY scope").fetchall()
            for scope, ing, cnt, first_e, last_e in rows_:
                fh = hours_since(ing)
                matrix.setdefault(scope, {})[label] = {
                    "fresh_h": fh, "fresh_sec": None if fh is None else round(fh * 3600),
                    "count": cnt, "first": first_e, "last": last_e}

        # full queue schedule (per source/kind/scope task, sorted by what's next)
        queue = []
        for kind, source, scope, status, last_ok, next_due, attempts, err in self.c.execute(
                "SELECT kind, source, scope, status, last_ok, next_due, attempts, last_error "
                "FROM collection_tasks ORDER BY scope, kind"):   # STABLE grid — no reshuffle
            try:
                due_in = round((datetime.fromisoformat(next_due) - now).total_seconds() / 60)
            except Exception:
                due_in = None
            queue.append({"kind": kind, "source": source, "scope": scope, "status": status,
                          "collected_h": hours_since(last_ok), "due_in_min": due_in,
                          "last_ok": last_ok, "next_due": next_due,
                          "attempts": attempts, "error": err})

        total_rows = self.c.execute("SELECT COUNT(*) FROM feature_values").fetchone()[0]
        all_total = sum(k["total"] for k in kinds)
        all_done = sum(k["collected"] for k in kinds)
        # honest headline: average of the per-kind coverage-vs-expectation %, NOT
        # "every task ran once". full = kinds actually at 100% of their expectation.
        avg_pct = round(sum(k["pct"] for k in kinds) / len(kinds)) if kinds else 0
        by_mode = {}
        for k in kinds:
            by_mode.setdefault(k["mode"], []).append(k["pct"])
        return {"generated_at": nowi, "quota": self.status()["quota"],
                "overall": {"tasks": all_total, "collected": all_done,
                            "pct": avg_pct,
                            "kinds_full": sum(1 for k in kinds if k["pct"] >= 100),
                            "kinds_total": len(kinds),
                            "by_mode": {m: round(sum(v) / len(v)) for m, v in by_mode.items()},
                            "data_points": total_rows, "due_now": self.status()["due_now"]},
                "kinds": kinds, "signals": signals, "matrix_cols": cols, "matrix": matrix,
                "queue": queue}


# ═══════════════ handlers — one unit of work each -> (n_rows, n_calls) ═════════
# yfinance (soft-limited; we pace politely). Reuse the S1 fetchers so there's no
# duplicate fetch logic and no train/serve skew.

def _reg_s1(store):
    import s1_data
    s1_data.register_all(store)


_TS = None


def _typed():
    """Module-singleton TypedStore — the proper typed tables (schema.py). Handlers
    write typed rows here; the scalar feature_values projection is kept only for
    the S2 features that read it."""
    global _TS
    if _TS is None:
        import schema
        _TS = schema.TypedStore()
    return _TS


def _h_bars(scope, store, tid):
    import s1_data
    _reg_s1(store)
    bars = s1_data.fetch_daily_bars([scope], period="1y").get(scope, [])
    rows = []
    for b in bars:
        rows.append(("price.close", scope, b["date"], b["close"]))
        rows.append(("price.volume", scope, b["date"], b["volume"]))
    if not rows:
        # empty = yfinance throttled/failed this ticker. RAISE so the task retries
        # (with backoff) instead of falsely marking itself collected with no data.
        raise RuntimeError("no bars returned (rate-limited?) — will retry")
    store.write_many(rows, trigger_id=tid)
    return len(rows), 1


def _h_bars_polygon(scope, store, tid):
    """HOURLY bars from Polygon (reliable — yfinance throttles rapid download bursts).
    One call per ticker for the full [COLLECTION_START, today] range. Stores every
    hourly bar in the typed `bars` table (bar_ts), and projects the DAILY EOD close /
    summed volume to feature_values so the daily S2/model consumers keep working."""
    key = load_secret("POLYGON_API_KEY")
    if not key:
        raise RuntimeError("no POLYGON_API_KEY")
    import s1_data
    _reg_s1(store)
    to = date.today().isoformat()
    frm = COLLECTION_START.isoformat()                        # fixed history horizon
    d = _poly_get(f"/v2/aggs/ticker/{scope}/range/1/hour/{frm}/{to}"
                  f"?adjusted=true&sort=asc&limit=50000", key)
    res = d.get("results") or []
    if not res:
        raise RuntimeError(f"no Polygon bars for {scope} — will retry")
    typed = []
    eod = {}                                                  # session date -> (last close, summed volume)
    for b in res:
        dtm = datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc)
        typed.append({"ticker": scope, "bar_ts": dtm.isoformat(),
                      "open": b.get("o"), "high": b.get("h"), "low": b.get("l"),
                      "close": b.get("c"), "volume": b.get("v")})
        day = dtm.date().isoformat()
        c, v = eod.get(day, (None, 0.0))
        eod[day] = (b.get("c"), v + (b.get("v") or 0))        # bars are sorted asc -> last close wins
    _typed().put_many("bars", typed)                          # TYPED: full hourly OHLCV
    proj = []
    for day, (c, v) in eod.items():                           # DAILY EOD projection for S2
        proj.append(("price.close", scope, day, c))
        proj.append(("price.volume", scope, day, v))
    store.write_many(proj, trigger_id=tid)
    return len(typed), 1


def _h_macro(scope, store, tid):
    import s1_data
    _reg_s1(store)
    span_days = (date.today() - COLLECTION_START).days + 5     # cover the fixed horizon
    macro = s1_data.fetch_macro(f"{span_days}d")
    typed = [{"name": k, "date": pt["date"], "value": pt["value"]}
             for k, series in macro.items() for pt in series]
    proj = [(f"macro.{k}", MARKET_SCOPE, pt["date"], pt["value"])
            for k, series in macro.items() for pt in series]
    _typed().put_many("macro", typed)                         # TYPED: name,date,value
    if proj:
        store.write_many(proj, trigger_id=tid)
    return len(typed), 3     # fetch_macro hits 3 symbols


def _h_quote(scope, store, tid):
    import s1_data
    _reg_s1(store)
    q = s1_data.fetch_current_quote(scope)
    if q is None:
        return 0, 1
    # quote_ts = when the quote was GENERATED (market timestamp), not today's date.
    ts = q.get("as_of") or datetime.now(timezone.utc).isoformat()
    _typed().put("quotes", {"ticker": scope, "quote_ts": ts,
                            "price": q["price"], "session": q["session"]})
    store.write("price.current", scope, ts, q, trigger_id=tid)   # projection for consumers
    return 1, 1


def _h_short(scope, store, tid):
    import s1_data
    _reg_s1(store)
    si = s1_data.fetch_short_interest(scope)
    if si is None:
        return 0, 1
    et = si["event_time"] or _today()
    _typed().put("short_interest", {"ticker": scope, "settlement_date": et,
                 "shares_short": si["shares_short"], "pct_float": si["pct_float"],
                 "days_to_cover": si["days_to_cover"], "change_pct": si["change_pct"]})
    for feat, key in (("short.shares", "shares_short"), ("short.pct_float", "pct_float"),
                      ("short.days_to_cover", "days_to_cover"), ("short.change_pct", "change_pct")):
        if si[key] is not None:
            store.write(feat, scope, et, si[key], trigger_id=tid)
    return 1, 1


def _h_analyst(scope, store, tid):
    import s1_data
    _reg_s1(store)
    an = s1_data.fetch_analyst_snapshot(scope)
    if an is None:
        return 0, 1
    store.write("fundamental.analyst_snapshot", scope, _today(), an, trigger_id=tid)
    return 1, 1


def _h_statements(scope, store, tid):
    import s1_data
    _reg_s1(store)
    st = s1_data.fetch_latest_statements(scope)
    sh = s1_data.fetch_shares_outstanding(scope)
    n = 0
    if sh is not None:
        store.write("fundamental.shares_outstanding", scope, _today(), sh, trigger_id=tid); n += 1
    if st is not None:
        # shares_outstanding travels WITH the statement at its publish_date, so S2 can
        # read a PIT-correct market cap at any historical date (the standalone snapshot
        # only accrues forward and is blank for older dates).
        st = {**st, "shares_outstanding": sh}
        _typed().put("fundamentals", {                       # TYPED: line items as columns
            "ticker": scope, "publish_date": st["event_time"], "period_end": st.get("period_end"),
            "revenue": st.get("revenue"), "net_income": st.get("net_income"),
            "total_equity": st.get("total_equity"), "gross_profit": st.get("gross_profit"),
            "total_assets": st.get("total_assets"), "free_cash_flow": st.get("free_cash_flow"),
            "trailing_eps": st.get("trailing_eps"), "shares_outstanding": sh})
        store.write("fundamental.statements", scope, st["event_time"], st, trigger_id=tid); n += 1
    return n, 1


def _h_insider(scope, store, tid):
    """RAW insider (SEC Form 4) transactions — ONE TYPED ROW PER TRANSACTION, each
    with its own transaction date. S2 derives net-sell metrics from these rows."""
    import yfinance as yf
    try:
        df = yf.Ticker(scope).insider_transactions
    except Exception:
        return 0, 1
    if df is None or len(df) == 0:
        return 0, 1
    rows = []
    for _, r in df.iterrows():
        d = r.get("Start Date")
        d = d.date().isoformat() if hasattr(d, "date") else (str(d)[:10] if d is not None else None)
        txt = str(r.get("Text", "")); val = _num(r.get("Value")) or 0.0
        rows.append({"ticker": scope, "txn_date": d, "value": val,
                     "shares": _num(r.get("Shares")), "position": str(r.get("Position", "")),
                     "insider": str(r.get("Insider", "")),
                     "is_sale": 1 if (("sale" in txt.lower()) and val > 0) else 0})
    return _typed().put_many("insider_transactions", rows), 1


def _h_analyst_revisions(scope, store, tid):
    """RAW analyst upgrades/downgrades — ONE TYPED ROW PER REVISION, each with its
    grade-change date. S2 derives net-revision momentum from these rows."""
    import yfinance as yf
    try:
        df = yf.Ticker(scope).upgrades_downgrades
    except Exception:
        return 0, 1
    if df is None or len(df) == 0:
        return 0, 1
    rows = []
    for idx, r in df.iterrows():
        d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
        rows.append({"ticker": scope, "revision_date": d,
                     "firm": str(r.get("Firm", "")), "action": str(r.get("Action", "")).lower(),
                     "from_grade": str(r.get("FromGrade", "")), "to_grade": str(r.get("ToGrade", ""))})
    return _typed().put_many("analyst_revisions", rows), 1


def _h_dte(scope, store, tid):
    import s1_data
    _reg_s1(store)
    dte = s1_data.fetch_days_to_earnings(scope)
    if dte is None:
        return 0, 1
    store.write("calendar.days_to_earnings", scope, _today(), dte, trigger_id=tid)
    return 1, 1


# Polygon options — implied move from the ATM straddle (basic plan: 5 calls/min).
_OPT_FEATURES = [
    ("opt.implied_move", "float", "ticker", "daily",
     "Options-implied earnings move = 0.85 x ATM straddle / underlying, first "
     "expiry >= today (Polygon). Forward-looking snapshot; event_time = the "
     "snapshot date; history accrues going forward."),
    ("opt.straddle_pct", "float", "ticker", "daily", "ATM straddle price / underlying."),
    ("opt.expiry", "string", "ticker", "daily", "Expiry used for the implied move."),
]


def _poly_get(path, key):
    import urllib.request
    sep = "&" if "?" in path else "?"
    url = f"https://api.polygon.io{path}{sep}apiKey={key}"
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.loads(r.read())


def _h_implied_move(scope, store, tid):
    key = load_secret("POLYGON_API_KEY")
    if not key:
        return 0, 0
    for name, dt, sk, cd, rule in _OPT_FEATURES:
        store.register(name, dt, sk, "S1", cd, rule)
    calls = 0
    # underlying: reuse the store's close if present, else 1 Polygon call
    last = store.read_series("price.close", scope, "2099-01-01", 1)
    px = last[-1][1] if last else None
    if px is None:
        u = _poly_get(f"/v2/aggs/ticker/{scope}/prev", key); calls += 1
        px = (u.get("results") or [{}])[0].get("c")
    if not px:
        return 0, calls
    lo = _today()
    c = _poly_get(f"/v3/reference/options/contracts?underlying_ticker={scope}"
                  f"&expiration_date.gte={lo}&limit=250&sort=expiration_date&order=asc", key); calls += 1
    res = c.get("results", [])
    if not res:
        return 0, calls
    expiry = res[0]["expiration_date"]
    same = [r for r in res if r["expiration_date"] == expiry]
    cc = [r for r in same if r.get("contract_type") == "call"]
    pp = [r for r in same if r.get("contract_type") == "put"]
    if not cc or not pp:
        return 0, calls
    atmc = min(cc, key=lambda r: abs(r["strike_price"] - px))
    atmp = min(pp, key=lambda r: abs(r["strike_price"] - px))
    a = _poly_get(f"/v2/aggs/ticker/{atmc['ticker']}/prev", key); calls += 1
    b = _poly_get(f"/v2/aggs/ticker/{atmp['ticker']}/prev", key); calls += 1
    cp = (a.get("results") or [{}])[0].get("c")
    ptp = (b.get("results") or [{}])[0].get("c")
    if cp is None or ptp is None:
        return 0, calls
    straddle = cp + ptp
    today = _today()
    _typed().put("options_implied", {"ticker": scope, "snap_date": today,
                 "underlying": px, "expiry": expiry, "atm_call": cp, "atm_put": ptp,
                 "straddle_pct": round(straddle / px, 4),
                 "implied_move": round(0.85 * straddle / px, 4)})
    store.write("opt.straddle_pct", scope, today, round(straddle / px, 4), trigger_id=tid)
    store.write("opt.implied_move", scope, today, round(0.85 * straddle / px, 4), trigger_id=tid)
    store.write("opt.expiry", scope, today, expiry, trigger_id=tid)
    return 3, calls


# ── EARNINGS: raw download -> processed analysis (a two-stage pipeline) ──
# earn_report DOWNLOADS the raw report data and stores it; earn_analysis (a
# dependent PROCESSING task) reads that raw record, computes the analysis, and
# stores the processed outcome. Report generation reads earnings.analysis.
_EARN_FEATURES = [
    ("earnings.report_raw", "json", "ticker", "event",
     "DOWNLOADED raw earnings report: EPS estimate/reported/surprise + latest "
     "quarterly revenue & net income. event_time = the earnings announcement date."),
    ("earnings.analysis", "json", "ticker", "event",
     "PROCESSED earnings analysis derived from earnings.report_raw: EPS surprise, "
     "revenue YoY, net margin, beat/miss, signal score. event_time = the report date."),
]


def _num(v):
    try:
        v = float(v)
        return v if v == v else None      # drop NaN
    except Exception:
        return None


def fetch_earnings_reports(ticker: str) -> list[dict]:
    """Download EVERY reported quarter yfinance exposes (typically 4–8), newest-first
    — NOT just the latest — and save the COMPLETE RAW as reported for that quarter:
    the full earnings-date calendar row + every income-statement line item, verbatim in
    `raw`. Only this-quarter figures; NO derived/cross-quarter fields (YoY, margins,
    beat/miss are S2). event_time = announcement date (PIT-correct). Event-frequency:
    collect all from the source, dedup by (ticker, report_date)."""
    import yfinance as yf
    from datetime import date
    try:
        tk = yf.Ticker(ticker)
        ed = tk.get_earnings_dates(limit=16)
    except Exception:
        return []
    if ed is None or len(ed) == 0:
        return []
    try:
        q = tk.quarterly_income_stmt
    except Exception:
        q = None

    def line(labels, col):
        if q is None or col >= q.shape[1]:
            return None
        for lab in labels:
            if lab in q.index:
                return _num(q.loc[lab].iloc[col])
        return None

    today, out, qi = date.today(), [], 0
    for idx, row in ed.iterrows():                    # sorted newest-first
        d = idx.date() if hasattr(idx, "date") else idx
        if d > today or _num(row.get("Reported EPS")) is None:
            continue                                  # only past, actually-reported quarters
        # COMPLETE raw payload for this quarter, verbatim from the source
        cal_row = {str(k): _num(v) for k, v in row.items()}
        inc_stmt = ({str(q.index[r]): _num(q.iloc[r, qi]) for r in range(len(q.index))}
                    if (q is not None and qi < q.shape[1]) else {})
        raw = {"event_time": d.isoformat(),
               "eps_estimate": _num(row.get("EPS Estimate")),
               "eps_reported": _num(row.get("Reported EPS")),
               "surprise_pct": _num(row.get("Surprise(%)")),
               "revenue": line(["Total Revenue", "Revenue"], qi),
               "net_income": line(["Net Income", "Net Income Common Stockholders"], qi),
               "gross_profit": line(["Gross Profit"], qi),
               "operating_income": line(["Operating Income", "Operating Income Or Loss"], qi),
               "raw": {"earnings_date_row": cal_row, "income_statement": inc_stmt}}
        out.append(raw)
        qi += 1
    return out


def analyze_earnings(raw: dict, prev_revenue: float | None = None) -> dict:
    """PROCESS a raw earnings report into the analysis outcome (S2). YoY is derived here
    from the year-ago quarter's raw revenue (`prev_revenue`), which S2 looks up from the
    raw store — it is NOT a field of the raw report."""
    s = raw.get("surprise_pct")
    rev, prev, ni = raw.get("revenue"), prev_revenue, raw.get("net_income")
    yoy = ((rev - prev) / prev * 100) if (rev and prev and prev > 0) else None
    margin = (ni / rev * 100) if (ni is not None and rev) else None
    beat = None if s is None else ("beat" if s > 0.5 else ("miss" if s < -0.5 else "inline"))
    score = None if s is None else round(max(-1.0, min(1.0, s / 10.0)), 3)   # ±10% surprise → ±1
    return {"report_event_time": raw.get("event_time"), "eps_surprise_pct": s,
            "revenue_yoy_pct": round(yoy, 2) if yoy is not None else None,
            "net_margin_pct": round(margin, 2) if margin is not None else None,
            "beat_miss": beat, "signal_score": score, "processed": True}


def _h_earn_report(scope, store, tid):
    for n, dt, sk, cd, r in _EARN_FEATURES:
        store.register(n, dt, sk, "S1", cd, r)
    reports = fetch_earnings_reports(scope)                   # ALL reported quarters
    if not reports:
        return 0, 1
    _typed().put_many("earnings_reports", [{                  # TYPED: one raw row per quarter
        "ticker": scope, "report_date": r["event_time"],
        "eps_estimate": r.get("eps_estimate"), "eps_reported": r.get("eps_reported"),
        "surprise_pct": r.get("surprise_pct"), "revenue": r.get("revenue"),
        "net_income": r.get("net_income"), "gross_profit": r.get("gross_profit"),
        "operating_income": r.get("operating_income"),
        "raw_json": json.dumps(r.get("raw", {}))}             # COMPLETE source payload
        for r in reports])
    for r in reports:                                         # full raw dict, one per event_time
        store.write("earnings.report_raw", scope, r["event_time"], r, trigger_id=tid)
    return len(reports), 1


def _h_sec_filings(scope, store, tid):
    """The ACTUAL earnings report as document text (SEC EDGAR): 8-K earnings releases +
    10-Q/10-K (financials tables + MD&A narrative). Event-frequency: collect all filings
    since COLLECTION_START, dedup by accession. Feeds the S2 NLP layer."""
    import s1_edgar
    rows, n_calls = s1_edgar.collect_filings(scope, COLLECTION_START.isoformat())
    if not rows:
        return 0, max(1, n_calls)
    _typed().put_many("sec_filings", rows)                    # raw_text = full filing text
    return len(rows), n_calls


def _h_xbrl(scope, store, tid):
    """Full financial-statement line items from EDGAR XBRL company-facts — every standard
    concept × period since COLLECTION_START (authoritative, complete)."""
    import s1_edgar
    rows, n_calls = s1_edgar.collect_xbrl(scope, COLLECTION_START.isoformat())
    if not rows:
        return 0, max(1, n_calls)
    _typed().put_many("xbrl_financials", rows)
    return len(rows), n_calls


def _h_transcript(scope, store, tid):
    """Earnings-call transcript (richest forward-looking narrative). PAID source — needs
    a provider + API key (Seeking Alpha / FMP / API Ninjas). Until one is configured this
    raises so the signal shows as BLOCKED (honest coverage), not a false success."""
    key = load_secret("TRANSCRIPT_API_KEY")
    if not key:
        raise RuntimeError("no transcript source configured — set TRANSCRIPT_API_KEY "
                           "and pick a provider (see docs/data-collection)")
    raise RuntimeError("transcript provider not yet implemented")   # provider TBD


def _h_earn_analysis(scope, store, tid):
    """PROCESSING task: read the downloaded raw report, analyze, store the outcome.
    Depends on earn_report — if the raw isn't collected yet, no-op and retry."""
    for n, dt, sk, cd, r in _EARN_FEATURES:
        store.register(n, dt, sk, "S1", cd, r)
    rec = store.read_asof("earnings.report_raw", scope, _today())
    if not rec:
        return 0, 0                                   # raw not downloaded yet
    raw = rec["value"]
    # YoY is derived HERE (S2) from the year-ago quarter's raw revenue: read the raw
    # report as-of ~350 days before this one -> the same quarter last year.
    prev_rev = None
    try:
        ya_asof = (date.fromisoformat(raw["event_time"]) - timedelta(days=350)).isoformat()
        ya = store.read_asof("earnings.report_raw", scope, ya_asof)
        if ya:
            prev_rev = ya["value"].get("revenue")
    except Exception:
        pass
    store.write("earnings.analysis", scope, raw["event_time"],
                analyze_earnings(raw, prev_rev), trigger_id=tid)
    return 1, 0                                       # local processing, no external call


# ── EARNINGS DATE: raw next-report date/time (S1) -> days_to_earnings (S2) ──
_CAL_FEATURES = [
    ("earnings.next_date", "json", "ticker", "daily",
     "RAW: exact scheduled date & time of the NEXT earnings report (from the "
     "earnings calendar). event_time = snapshot day; issuer-revisable."),
]


def fetch_next_earnings_date(ticker: str) -> dict | None:
    import yfinance as yf
    from datetime import date
    try:
        ed = yf.Ticker(ticker).get_earnings_dates(limit=8)
    except Exception:
        return None
    if ed is None or len(ed) == 0:
        return None
    today = date.today()
    fut = [idx for idx in ed.index if (idx.date() if hasattr(idx, "date") else idx) >= today]
    if not fut:
        return None
    nxt = min(fut)
    try:
        iso = nxt.isoformat()
    except Exception:
        iso = str(nxt)
    return {"next_earnings": iso}


def _h_earn_date(scope, store, tid):
    for n, dt, sk, cd, r in _CAL_FEATURES:
        store.register(n, dt, sk, "S1", cd, r)
    v = fetch_next_earnings_date(scope)
    if not v:
        return 0, 1
    _typed().put("earnings_calendar", {"ticker": scope, "snap_date": _today(),
                 "next_earnings_ts": v["next_earnings"]})     # TYPED: exact next-earnings ts
    store.write("earnings.next_date", scope, _today(), v, trigger_id=tid)
    return 1, 1


def _h_days_to_earn(scope, store, tid):
    """S2 PROCESSING: derive days_to_earnings from the raw earnings.next_date."""
    import s1_data
    s1_data.register_all(store)                      # registers calendar.days_to_earnings
    rec = store.read_asof("earnings.next_date", scope, _today())
    if not rec:
        return 0, 0
    from datetime import date
    try:
        y, m, d = map(int, rec["value"]["next_earnings"][:10].split("-"))
        days = (date(y, m, d) - date.today()).days
    except Exception:
        return 0, 0
    store.write("calendar.days_to_earnings", scope, _today(), days, trigger_id=tid)
    return 1, 0


def _today():
    return datetime.now(timezone.utc).date().isoformat()


# ═══════════════ default wiring ════════════════════════════════════════════════
def default_collector(db_path=DEFAULT_DB, store=None, now_fn=None) -> Collector:
    col = Collector(db_path, store, now_fn)
    col.register_source("yfinance", limit=30, window_sec=60)    # gentle — yfinance throttles bursts
    col.register_source("polygon", limit=5, window_sec=60)      # basic plan hard cap
    col.register_source("process", limit=10000, window_sec=60)  # local CPU (derived signals)
    # SEC EDGAR: 60 calls / 10s window = 6/s sustained budget (a multi-call filings task
    # fits); the handler paces ~0.12s/call so bursts stay under SEC's 10 req/s cap.
    col.register_source("sec", limit=60, window_sec=10)
    col.register_source("transcript", limit=5, window_sec=60)   # paid transcript API
    # kind, source, interval, priority, handler, scope, est_calls
    # kind, source, priority, handler, frequency=…  (poll interval defaults to cadence)
    col.register_kind("macro", "yfinance", 10, _h_macro, frequency="daily", scope="market", est_calls=3)
    col.register_kind("bars", "polygon", 20, _h_bars_polygon, frequency="hourly", est_calls=1)  # intraday OHLCV
    col.register_kind("implied_move", "polygon", 25, _h_implied_move, frequency="snapshot", est_calls=4)
    # quote polled every 5 min; fresh ≤5m green, ≤30m yellow, >30m red
    col.register_kind("quote", "yfinance", 30, _h_quote, frequency="snapshot",
                      interval_sec=300, fresh_sla=(300, 1800))
    col.register_kind("analyst", "yfinance", 40, _h_analyst, frequency="snapshot")
    col.register_kind("analyst_revisions", "yfinance", 41, _h_analyst_revisions, frequency="event")
    col.register_kind("short", "yfinance", 42, _h_short, frequency="snapshot", interval_sec=3 * DAY)
    col.register_kind("insider", "yfinance", 44, _h_insider, frequency="event", interval_sec=3 * DAY)
    col.register_kind("earn_date", "yfinance", 45, _h_earn_date, frequency="snapshot")   # next-earnings date/time
    col.register_kind("earn_report", "yfinance", 46, _h_earn_report, frequency="event")
    col.register_kind("statements", "yfinance", 50, _h_statements, frequency="event")
    # the REAL earnings report as document text + full financials (SEC EDGAR, free)
    col.register_kind("sec_filings", "sec", 47, _h_sec_filings, frequency="event", est_calls=12)
    col.register_kind("xbrl", "sec", 48, _h_xbrl, frequency="event", est_calls=1)
    col.register_kind("transcript", "transcript", 49, _h_transcript, frequency="event")  # PAID, blocked until key set
    # PROCESSED (derived) — S2 signal generation, NOT S1 collection. Tagged S2 so
    # they don't show on the data-collection dashboard. Each reads raw S1 data.
    col.register_kind("earn_analysis", "process", 55, _h_earn_analysis, frequency="daily", stage="S2")
    col.register_kind("days_to_earn", "process", 56, _h_days_to_earn, frequency="daily", stage="S2")
    return col


def _single_instance_lock():
    """Hold an exclusive lock so a cron drain and a running daemon never double-run
    and race the shared rate limiter. Returns the open file (keep the reference) or
    None if another worker already holds it."""
    import fcntl
    lock_path = Path(DEFAULT_DB).parent / "collector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        return None


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="S1 queue-driven collector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    sub.add_parser("reconcile")
    p_add = sub.add_parser("add-ticker"); p_add.add_argument("ticker")
    p_dr = sub.add_parser("drain"); p_dr.add_argument("--seconds", type=float, default=55)
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("coverage")
    args = ap.parse_args()
    col = default_collector()
    if args.cmd in ("seed", "reconcile"):
        n = col.reconcile()
        print(f"reconciled: armed {n} prioritized backfill task(s)")
        print(json.dumps(col.status(), indent=2, default=str))
    elif args.cmd == "add-ticker":
        col.add_ticker(args.ticker.upper()); print(f"enqueued backfill for {args.ticker.upper()}")
    elif args.cmd == "drain":
        lock = _single_instance_lock()
        if lock is None:
            print("another collector worker is running — skipping"); return
        print(json.dumps(col.drain(seconds=args.seconds), default=str))
    elif args.cmd == "run":
        lock = _single_instance_lock()
        if lock is None:
            print("another collector worker is already running — exiting"); return
        print("collector daemon starting (Ctrl-C to stop)"); col.run_forever()
    elif args.cmd == "status":
        print(json.dumps(col.status(), indent=2, default=str))
    elif args.cmd == "coverage":
        r = col.coverage_report(); o = r["overall"]
        print(f'COVERAGE vs expectation: {o["pct"]}%  ·  {o.get("kinds_full",0)}/'
              f'{o.get("kinds_total",0)} signals full  ·  by mode {o.get("by_mode",{})}')
        print(f'{"KIND":19}{"MODE":9}{"COV":>5}   WHAT WE HOLD vs EXPECTED')
        for k in r["kinds"]:
            print(f'{k["kind"]:19}{k["mode"]:9}{k["pct"]:>4}%   {k.get("detail","")}')


if __name__ == "__main__":
    _cli()
