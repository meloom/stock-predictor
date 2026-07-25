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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import FeatureStore, DEFAULT_DB, MARKET_SCOPE   # noqa: E402

DAY = 86400
CREDENTIALS = Path.home() / ".credentials"

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

    def register_kind(self, kind, source, interval_sec, priority, handler,
                      scope="ticker", est_calls=1):
        """Declare a data kind: its source, refresh cadence, priority, handler
        fn(scope, store, trigger_id)->(n_rows, n_calls), scope type, and how many
        API calls one unit costs (for strict rate limiting)."""
        self.kinds[kind] = {"source": source, "interval": interval_sec,
                            "priority": priority, "scope": scope, "est_calls": est_calls}
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
                self._reschedule(task_id, interval, now, n_rows)
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

    def run_forever(self, sleep: float = 2.0):
        while True:
            if self.tick() is None:
                time.sleep(sleep)

    # ── task state transitions ────────────────────────────────────────────────
    def _reschedule(self, task_id, interval, now, n_rows):
        nowi = now.isoformat()
        if interval:
            nxt = (now + timedelta(seconds=interval)).isoformat()
            self.c.execute("UPDATE collection_tasks SET status='pending', next_due=?, attempts=0, "
                           "last_ok=?, last_error=NULL, updated_at=? WHERE task_id=?",
                           (nxt, nowi, nowi, task_id))
        else:
            self.c.execute("UPDATE collection_tasks SET status='done', attempts=0, last_ok=?, "
                           "updated_at=? WHERE task_id=?", (nowi, nowi, task_id))
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
        stamps = [{"event_time": et, "ingested_at": ing} for et, ing in self.c.execute(
            "SELECT event_time, ingested_at FROM feature_values WHERE feature=? AND scope=? "
            "ORDER BY ingested_at DESC LIMIT ?", (feature, scope, ts_limit)).fetchall()]
        total = self.c.execute("SELECT COUNT(*) FROM feature_values WHERE feature=? AND scope=?",
                               (feature, scope)).fetchone()[0]
        span = self.c.execute("SELECT MIN(event_time), MAX(event_time) FROM feature_values "
                              "WHERE feature=? AND scope=?", (feature, scope)).fetchone()
        return {"scope": scope, "feature": feature, "total": total,
                "first_event": span[0], "last_event": span[1],
                "daily": list(reversed(daily)), "stamps": stamps}

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

        # per-kind queue progress (backfill = collected at least once)
        kinds = []
        for kind, total, collected, due, errs, last in self.c.execute(
                "SELECT kind, COUNT(*), SUM(last_ok IS NOT NULL), "
                "SUM(status='pending' AND next_due<=?), SUM(last_error IS NOT NULL), MAX(last_ok) "
                "FROM collection_tasks GROUP BY kind", (nowi,)):
            kinds.append({"kind": kind, "source": self.kinds.get(kind, {}).get("source", "?"),
                          "total": total, "collected": collected or 0, "due_now": due or 0,
                          "errors": errs or 0, "pct": round(100 * (collected or 0) / total) if total else 0,
                          "last_run_h": hours_since(last)})
        # STABLE order (by collection priority, then name) — never reorder by progress,
        # so rows don't jump around as the live page refreshes.
        kinds.sort(key=lambda k: (self.kinds.get(k["kind"], {}).get("priority", 999), k["kind"]))

        # per-signal store coverage
        signals = []
        for feature, nrows, nscopes, latest_evt, latest_ing in self.c.execute(
                "SELECT feature, COUNT(*), COUNT(DISTINCT scope), MAX(event_time), MAX(ingested_at) "
                "FROM feature_values GROUP BY feature ORDER BY feature"):
            signals.append({"feature": feature, "rows": nrows, "scopes": nscopes,
                            "latest_event": latest_evt, "fresh_h": hours_since(latest_ing)})

        # ticker × signal matrix, with the TIME detail per cell (count + event span)
        cols = matrix_features or ["price.close", "price.current", "short.pct_float",
                                   "opt.implied_move", "fundamental.analyst_snapshot",
                                   "fundamental.statements", "calendar.days_to_earnings"]
        matrix = {}
        for feature, scope, ing, cnt, first_e, last_e in self.c.execute(
                "SELECT feature, scope, MAX(ingested_at), COUNT(*), MIN(event_time), MAX(event_time) "
                "FROM feature_values WHERE feature IN (%s) GROUP BY feature, scope"
                % ",".join("?" * len(cols)), cols):
            matrix.setdefault(scope, {})[feature] = {
                "fresh_h": hours_since(ing), "count": cnt, "first": first_e, "last": last_e}

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
                          "attempts": attempts, "error": err})

        total_rows = self.c.execute("SELECT COUNT(*) FROM feature_values").fetchone()[0]
        all_total = sum(k["total"] for k in kinds)
        all_done = sum(k["collected"] for k in kinds)
        return {"generated_at": nowi, "quota": self.status()["quota"],
                "overall": {"tasks": all_total, "collected": all_done,
                            "pct": round(100 * all_done / all_total) if all_total else 0,
                            "data_points": total_rows, "due_now": self.status()["due_now"]},
                "kinds": kinds, "signals": signals, "matrix_cols": cols, "matrix": matrix,
                "queue": queue}


# ═══════════════ handlers — one unit of work each -> (n_rows, n_calls) ═════════
# yfinance (soft-limited; we pace politely). Reuse the S1 fetchers so there's no
# duplicate fetch logic and no train/serve skew.

def _reg_s1(store):
    import s1_data
    s1_data.register_all(store)


def _h_bars(scope, store, tid):
    import s1_data
    _reg_s1(store)
    bars = s1_data.fetch_daily_bars([scope], period="1y").get(scope, [])
    rows = []
    for b in bars:
        rows.append(("price.close", scope, b["date"], b["close"]))
        rows.append(("price.volume", scope, b["date"], b["volume"]))
    if rows:
        store.write_many(rows, trigger_id=tid)
    return len(rows), 1


def _h_macro(scope, store, tid):
    import s1_data
    _reg_s1(store)
    macro = s1_data.fetch_macro("1y")
    rows = [(f"macro.{k}", MARKET_SCOPE, pt["date"], pt["value"])
            for k, series in macro.items() for pt in series]
    if rows:
        store.write_many(rows, trigger_id=tid)
    return len(rows), 3     # fetch_macro hits 3 symbols


def _h_quote(scope, store, tid):
    import s1_data
    _reg_s1(store)
    q = s1_data.fetch_current_quote(scope)
    if q is None:
        return 0, 1
    store.write("price.current", scope, _today(), q, trigger_id=tid)
    return 1, 1


def _h_short(scope, store, tid):
    import s1_data
    _reg_s1(store)
    si = s1_data.fetch_short_interest(scope)
    if si is None:
        return 0, 1
    et = si["event_time"] or _today()
    n = 0
    for feat, key in (("short.shares", "shares_short"), ("short.pct_float", "pct_float"),
                      ("short.days_to_cover", "days_to_cover"), ("short.change_pct", "change_pct")):
        if si[key] is not None:
            store.write(feat, scope, et, si[key], trigger_id=tid); n += 1
    return n, 1


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
        store.write("fundamental.statements", scope, st["event_time"], st, trigger_id=tid); n += 1
    return n, 1


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


def fetch_earnings_report(ticker: str) -> dict | None:
    """Download the raw earnings report for the most recent REPORTED quarter:
    EPS estimate/reported/surprise (from the earnings calendar) + latest quarterly
    revenue & net income. event_time = the announcement date (PIT-correct)."""
    import yfinance as yf
    from datetime import date
    try:
        tk = yf.Ticker(ticker)
        ed = tk.get_earnings_dates(limit=12)
    except Exception:
        return None
    if ed is None or len(ed) == 0:
        return None
    today = date.today()
    rep = None
    for idx, row in ed.iterrows():                    # sorted newest-first
        d = idx.date() if hasattr(idx, "date") else idx
        if d <= today and _num(row.get("Reported EPS")) is not None:
            rep = (d, row); break
    if rep is None:
        return None
    d, row = rep
    raw = {"event_time": d.isoformat(),
           "eps_estimate": _num(row.get("EPS Estimate")),
           "eps_reported": _num(row.get("Reported EPS")),
           "surprise_pct": _num(row.get("Surprise(%)"))}
    try:
        q = tk.quarterly_income_stmt
        if q is not None and q.shape[1] >= 1:
            def line(labels, col):
                for lab in labels:
                    if lab in q.index:
                        return _num(q.loc[lab].iloc[col])
                return None
            raw["revenue"] = line(["Total Revenue", "Revenue"], 0)
            raw["net_income"] = line(["Net Income", "Net Income Common Stockholders"], 0)
            if q.shape[1] >= 5:
                raw["revenue_year_ago"] = line(["Total Revenue", "Revenue"], 4)
    except Exception:
        pass
    return raw


def analyze_earnings(raw: dict) -> dict:
    """PROCESS a raw earnings report into the analysis outcome."""
    s = raw.get("surprise_pct")
    rev, prev, ni = raw.get("revenue"), raw.get("revenue_year_ago"), raw.get("net_income")
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
    raw = fetch_earnings_report(scope)
    if not raw or not raw.get("event_time"):
        return 0, 1
    store.write("earnings.report_raw", scope, raw["event_time"], raw, trigger_id=tid)
    return 1, 1


def _h_earn_analysis(scope, store, tid):
    """PROCESSING task: read the downloaded raw report, analyze, store the outcome.
    Depends on earn_report — if the raw isn't collected yet, no-op and retry."""
    for n, dt, sk, cd, r in _EARN_FEATURES:
        store.register(n, dt, sk, "S1", cd, r)
    rec = store.read_asof("earnings.report_raw", scope, _today())
    if not rec:
        return 0, 0                                   # raw not downloaded yet
    raw = rec["value"]
    store.write("earnings.analysis", scope, raw["event_time"], analyze_earnings(raw), trigger_id=tid)
    return 1, 0                                       # local processing, no external call


def _today():
    return datetime.now(timezone.utc).date().isoformat()


# ═══════════════ default wiring ════════════════════════════════════════════════
def default_collector(db_path=DEFAULT_DB, store=None, now_fn=None) -> Collector:
    col = Collector(db_path, store, now_fn)
    col.register_source("yfinance", limit=120, window_sec=60)   # polite self-limit
    col.register_source("polygon", limit=5, window_sec=60)      # basic plan hard cap
    col.register_source("process", limit=10000, window_sec=60)  # local CPU (derived signals)
    # kind, source, interval, priority, handler, scope, est_calls
    col.register_kind("macro", "yfinance", DAY, 10, _h_macro, scope="market", est_calls=3)
    col.register_kind("bars", "yfinance", DAY, 20, _h_bars, est_calls=1)
    col.register_kind("implied_move", "polygon", DAY, 25, _h_implied_move, est_calls=4)
    col.register_kind("quote", "yfinance", 21600, 30, _h_quote, est_calls=1)
    col.register_kind("analyst", "yfinance", DAY, 40, _h_analyst, est_calls=1)
    col.register_kind("short", "yfinance", 3 * DAY, 42, _h_short, est_calls=1)
    col.register_kind("dte", "yfinance", DAY, 45, _h_dte, est_calls=1)
    col.register_kind("earn_report", "yfinance", DAY, 46, _h_earn_report, est_calls=1)
    col.register_kind("statements", "yfinance", DAY, 50, _h_statements, est_calls=1)
    # PROCESSED (derived) — runs AFTER earn_report; reads the raw, writes the analysis
    col.register_kind("earn_analysis", "process", DAY, 55, _h_earn_analysis, est_calls=1)
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
    p_add = sub.add_parser("add-ticker"); p_add.add_argument("ticker")
    p_dr = sub.add_parser("drain"); p_dr.add_argument("--seconds", type=float, default=55)
    sub.add_parser("run")
    sub.add_parser("status")
    args = ap.parse_args()
    col = default_collector()
    if args.cmd == "seed":
        from universe import UNIVERSE
        col.seed(UNIVERSE); print(json.dumps(col.status(), indent=2, default=str))
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


if __name__ == "__main__":
    _cli()
