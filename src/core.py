"""core.py — cross-cutting infrastructure: trigger context + feature store.

Two things every stage depends on, in one file (project style: as few files
as possible; simple, working):

  1. Trigger — the unit of cost attribution and run logging. Mints a
     trigger_id carried by every billable action and feature write it
     initiates. Logs: runtime/logs/runs.jsonl (per run, INCLUDING crashes)
     and runtime/logs/cost_ledger.jsonl (per billable action).
  2. FeatureStore — registry-first bitemporal store (DESIGN.md contract):
     (event_time, ingested_at) keys, as_known_at reads make backtest
     lookahead impossible by construction, outputs_of(trigger_id) answers
     "we triggered it — what was the output?".
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

RUNTIME_DIR = Path(os.environ.get(
    "STOCK_PREDICTOR_RUNTIME",
    Path(__file__).resolve().parents[1] / "runtime",
))
LOGS_DIR = RUNTIME_DIR / "logs"
DEFAULT_DB = RUNTIME_DIR / "features.db"

MARKET_SCOPE = "_market"  # scope value for market-level (non-per-ticker) features

# ═══════════════════════════ Trigger + cost ledger ═══════════════════════════

# Estimated unit prices, USD — for the *relative* cost-per-trigger regression
# signal; the billing dashboard remains ground truth for absolute spend.
PRICING = {
    "claude-sonnet": {"per_m_tokens_in": 3.00, "per_m_tokens_out": 15.00},
    "web_search": {"per_call": 0.01},
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")

class Trigger:
    """Context manager for one pipeline invocation.

    Usage:
        with Trigger("daily_ingestion", stage="S1") as trig:
            ...
            trig.record_cost(provider="claude-sonnet", tokens_in=..., tokens_out=...)
            trig.add_metrics(tickers_ok=140, tickers_failed=2)
    On exit, writes the run record (status="ok" or "error" with the exception).
    """

    def __init__(self, trigger_type: str, stage: str):
        self.trigger_id = f"{trigger_type}-{uuid.uuid4().hex[:12]}"
        self.trigger_type = trigger_type
        self.stage = stage
        self.started_at = _utcnow()
        self.metrics: dict = {}
        self._cost_usd = 0.0

    # -- cost ledger ---------------------------------------------------------
    def record_cost(self, provider: str, tokens_in: int = 0, tokens_out: int = 0,
                    web_searches: int = 0, commission: float = 0.0,
                    note: str = "") -> float:
        prices = PRICING.get(provider, {})
        unit_cost = (
            tokens_in / 1e6 * prices.get("per_m_tokens_in", 0.0)
            + tokens_out / 1e6 * prices.get("per_m_tokens_out", 0.0)
            + web_searches * PRICING["web_search"]["per_call"]
        )
        self._cost_usd += unit_cost + commission
        _append_jsonl(LOGS_DIR / "cost_ledger.jsonl", {
            "trigger_id": self.trigger_id,
            "trigger_type": self.trigger_type,
            "stage": self.stage,
            "provider": provider,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "web_searches": web_searches,
            "unit_cost": round(unit_cost, 6),
            "commission": commission,
            "note": note,
            "ts": _utcnow(),
        })
        return unit_cost

    # -- run log -------------------------------------------------------------
    def add_metrics(self, **kwargs) -> None:
        self.metrics.update(kwargs)

    def _log_run(self, status: str, error: str = "") -> None:
        _append_jsonl(LOGS_DIR / "runs.jsonl", {
            "trigger_id": self.trigger_id,
            "trigger_type": self.trigger_type,
            "stage": self.stage,
            "started_at": self.started_at,
            "finished_at": _utcnow(),
            "status": status,
            "error": error,
            "cost_usd": round(self._cost_usd, 6),
            "metrics": self.metrics,
        })

    # -- context manager -----------------------------------------------------
    def __enter__(self) -> "Trigger":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            self._log_run("ok")
        else:
            self._log_run("error", error=f"{exc_type.__name__}: {exc}")
        return False  # never swallow exceptions

# ═══════════════════════════════ Feature store ═══════════════════════════════

_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry (
    name          TEXT PRIMARY KEY,
    dtype         TEXT NOT NULL,
    scope_kind    TEXT NOT NULL,          -- 'ticker' | 'market'
    source_stage  TEXT NOT NULL,          -- 'S1' | 'S2' | ...
    cadence       TEXT NOT NULL,          -- 'daily' | 'event' | ...
    pit_rule      TEXT NOT NULL,          -- human-readable point-in-time rule
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feature_values (
    feature     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    event_time  TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    value       TEXT NOT NULL,            -- JSON-encoded
    trigger_id  TEXT NOT NULL,
    PRIMARY KEY (feature, scope, event_time, ingested_at)
);
CREATE INDEX IF NOT EXISTS idx_read
    ON feature_values (feature, scope, event_time DESC, ingested_at DESC);
"""


class UnregisteredFeatureError(Exception):
    pass


class FeatureStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=60)
        # WAL + long busy-timeout so concurrent processes (collector daemon, dashboard,
        # S2/S3 jobs, manual scripts) don't hit "database is locked" — writers wait.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=60000")
        self._conn.executescript(_SCHEMA)
        # Lock the file to owner-only so no other user can bypass DataAPI by
        # opening the SQLite file directly.
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        self._registry_cache: set[str] = {
            r[0] for r in self._conn.execute("SELECT name FROM registry")
        }

    # -- registry ------------------------------------------------------------
    def register(self, name: str, dtype: str, scope_kind: str,
                 source_stage: str, cadence: str, pit_rule: str) -> None:
        """Idempotent: re-registering an existing name with identical spec is a
        no-op; changing an existing spec raises (append a NEW feature name for
        semantic changes — never mutate meaning under an old name)."""
        row = self._conn.execute(
            "SELECT dtype, scope_kind, source_stage, cadence, pit_rule "
            "FROM registry WHERE name=?", (name,)).fetchone()
        spec = (dtype, scope_kind, source_stage, cadence, pit_rule)
        if row is not None:
            if tuple(row) != spec:
                raise ValueError(
                    f"Feature {name!r} already registered with a different spec; "
                    f"register a new name instead of mutating meaning.")
            return
        self._conn.execute(
            "INSERT INTO registry VALUES (?,?,?,?,?,?,?)",
            (name, *spec, datetime.now(timezone.utc).isoformat()))
        self._conn.commit()
        self._registry_cache.add(name)

    def registry(self) -> list[dict]:
        cols = ["name", "dtype", "scope_kind", "source_stage", "cadence",
                "pit_rule", "registered_at"]
        return [dict(zip(cols, r)) for r in
                self._conn.execute("SELECT * FROM registry ORDER BY name")]

    # -- write ---------------------------------------------------------------
    def write(self, feature: str, scope: str, event_time: str, value: Any,
              trigger_id: str, ingested_at: Optional[str] = None) -> None:
        if feature not in self._registry_cache:
            raise UnregisteredFeatureError(
                f"{feature!r} is not registered — register() before writing "
                f"(DESIGN.md feature-store contract: unregistered writes rejected)")
        ingested_at = ingested_at or datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR REPLACE INTO feature_values VALUES (?,?,?,?,?,?)",
            (feature, scope, event_time, ingested_at,
             json.dumps(value), trigger_id))
        self._conn.commit()

    def write_if_changed(self, feature: str, scope: str, event_time: str, value: Any,
                         trigger_id: str, ingested_at: Optional[str] = None) -> bool:
        """Write ONLY if the latest stored value for this exact (feature, scope,
        event_time) differs — so polling slow-changing data (short interest, statements)
        hourly doesn't accrue identical bitemporal rows. Returns True iff it wrote."""
        if feature not in self._registry_cache:
            raise UnregisteredFeatureError(f"{feature!r} is not registered")
        row = self._conn.execute(
            "SELECT value FROM feature_values WHERE feature=? AND scope=? AND event_time=? "
            "ORDER BY ingested_at DESC LIMIT 1", (feature, scope, event_time)).fetchone()
        if row is not None and row[0] == json.dumps(value):
            return False
        self.write(feature, scope, event_time, value, trigger_id, ingested_at)
        return True

    def write_many(self, rows: list[tuple], trigger_id: str,
                   ingested_at: Optional[str] = None) -> int:
        """rows: [(feature, scope, event_time, value), ...] — one transaction."""
        ingested_at = ingested_at or datetime.now(timezone.utc).isoformat()
        for feature, *_ in rows:
            if feature not in self._registry_cache:
                raise UnregisteredFeatureError(f"{feature!r} is not registered")
        self._conn.executemany(
            "INSERT OR REPLACE INTO feature_values VALUES (?,?,?,?,?,?)",
            [(f, s, et, ingested_at, json.dumps(v), trigger_id)
             for f, s, et, v in rows])
        self._conn.commit()
        return len(rows)

    # -- read (the one API) --------------------------------------------------
    def read_series(self, feature: str, scope: str, end_event_time: str,
                    n: int, as_known_at: Optional[str] = None) -> list[tuple]:
        """Last n values with event_time <= end_event_time, ascending, one
        (latest-ingested) version per event_time. History reads for derived-
        feature computation (S2) go through this — same as_known_at semantics
        as read_asof, so a signal computed 'as known at T' is lookahead-free
        by construction. Returns [(event_time, value), ...]."""
        # SQLite documented behavior: with a single MAX() aggregate, bare
        # columns come from the max row — newest ingested_at per event_time
        # wins (bitemporal: corrections shadow older versions).
        q = ("SELECT event_time, value, MAX(ingested_at) FROM feature_values "
             "WHERE feature=? AND scope=? AND event_time<=?")
        params: list = [feature, scope, end_event_time]
        if as_known_at is not None:
            q += " AND ingested_at<=?"
            params.append(as_known_at)
        q += " GROUP BY event_time ORDER BY event_time DESC LIMIT ?"
        params.append(n)
        rows = self._conn.execute(q, params).fetchall()
        return [(et, json.loads(v)) for et, v, _ in reversed(rows)]

    def read_asof(self, feature: str, scope: str, event_time: str,
                  as_known_at: Optional[str] = None) -> Optional[dict]:
        """Latest value with event_time <= the requested time; if as_known_at
        is given, only versions ingested by then are visible (this is what
        makes backtests lookahead-impossible by construction)."""
        q = ("SELECT event_time, ingested_at, value, trigger_id FROM feature_values "
             "WHERE feature=? AND scope=? AND event_time<=?")
        params: list = [feature, scope, event_time]
        if as_known_at is not None:
            q += " AND ingested_at<=?"
            params.append(as_known_at)
        q += " ORDER BY event_time DESC, ingested_at DESC LIMIT 1"
        row = self._conn.execute(q, params).fetchone()
        if row is None:
            return None
        return {"feature": feature, "scope": scope, "event_time": row[0],
                "ingested_at": row[1], "value": json.loads(row[2]),
                "trigger_id": row[3]}

    def read_panel(self, feature: str, event_time: str,
                   as_known_at: Optional[str] = None) -> dict[str, dict]:
        """read_asof across every scope that has this feature — one query."""
        scopes = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT scope FROM feature_values WHERE feature=?", (feature,))]
        out = {}
        for s in scopes:
            rec = self.read_asof(feature, s, event_time, as_known_at)
            if rec is not None:
                out[s] = rec
        return out

    # -- lineage / audit (consumed by S8) ------------------------------------
    def outputs_of(self, trigger_id: str) -> dict:
        """Everything a trigger wrote — the answer to 'we triggered the
        component; what was the output?'. Pairs with the run record in
        runs.jsonl (status/metrics/cost) to give complete per-trigger
        observability: one lookup for what happened, one for what it produced."""
        rows = self._conn.execute(
            "SELECT feature, COUNT(*), MIN(event_time), MAX(event_time) "
            "FROM feature_values WHERE trigger_id=? "
            "GROUP BY feature ORDER BY feature", (trigger_id,)).fetchall()
        return {
            "trigger_id": trigger_id,
            "features": [
                {"feature": f, "n_values": n,
                 "event_time_min": lo, "event_time_max": hi}
                for f, n, lo, hi in rows
            ],
            "total_values": sum(r[1] for r in rows),
        }

    def freshness(self, feature: str) -> Optional[dict]:
        """Most recent event_time + ingested_at for a feature, for staleness checks."""
        row = self._conn.execute(
            "SELECT MAX(event_time), MAX(ingested_at) FROM feature_values "
            "WHERE feature=?", (feature,)).fetchone()
        if row is None or row[0] is None:
            return None
        return {"feature": feature, "latest_event_time": row[0],
                "latest_ingested_at": row[1]}

    def close(self) -> None:
        self._conn.close()


# ═══════════════════════════ Gated data-retrieval API ════════════════════════

class DataAPIError(Exception):
    """Base for all gated-read rejections."""


class UnknownSignalError(DataAPIError):
    pass


class UnknownScopeError(DataAPIError):
    pass


class InvalidTimeRangeError(DataAPIError):
    pass


_TICKER_RE = re.compile(r"^[A-Za-z0-9._\-]{1,24}$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def harden_db_permissions(db_path: Path | str = DEFAULT_DB) -> None:
    """Lock the DB file to owner-only (chmod 600) so no other user can open it.
    All same-user processes must go through DataAPI for reads. (SQLite is a file,
    so this — plus the read-only API handle — is the enforceable boundary; there
    is no engine-level ACL.)"""
    for p in (Path(db_path), Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass


class DataAPI:
    """The ONE sanctioned way to read stored signal data.

    Every consumer — the dashboard, S2/S3/S4, notebooks, ad-hoc scripts — must
    retrieve data through `get(ticker, signal, time_start, time_end)`. The class
    enforces that:

      • the handle is opened READ-ONLY (`mode=ro`), so a caller physically
        cannot mutate the store through it;
      • the DB file is chmod 600 (owner-only) — no external user can open it;
      • `signal` is validated against the registry whitelist (no reading an
        unregistered/typo'd field, no fishing);
      • `ticker` is format-checked and existence-checked;
      • the time range is parsed/ordered and the SQL is fully parameterized, so
        no caller can inject SQL or widen the query beyond one (ticker, signal).

    Returns bitemporal point-in-time-correct rows: one latest-ingested version
    per event_time, ascending. Pass `as_known_at` to read the store *as it was
    known* at a past instant (lookahead-free backtests)."""

    def __init__(self, db_path: Path | str = DEFAULT_DB, harden: bool = True):
        self._path = Path(db_path)
        if not self._path.exists():
            raise DataAPIError(f"no database at {self._path}")
        if harden:
            harden_db_permissions(self._path)
        # read-only URI handle: reads see committed WAL data; writes are impossible.
        self._conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True, timeout=60)
        self._conn.execute("PRAGMA busy_timeout=60000")
        self._signals = {r[0] for r in self._conn.execute("SELECT name FROM registry")}

    def signals(self) -> list[str]:
        """The whitelist of retrievable signals (registered feature names)."""
        return sorted(self._signals)

    def scopes(self, signal: str) -> list[str]:
        """Tickers that actually have data for a signal."""
        if signal not in self._signals:
            raise UnknownSignalError(f"{signal!r} is not a registered signal")
        return [r[0] for r in self._conn.execute(
            "SELECT DISTINCT scope FROM feature_values WHERE feature=? ORDER BY scope",
            (signal,))]

    @staticmethod
    def _parse_time(t: Any, *, end: bool = False) -> str:
        """Validate an ISO date/datetime string and return it for lexical compare.
        A date-only *end* bound is widened to end-of-day so `get(...,'2026-07-27')`
        includes that whole day's intraday rows."""
        s = str(t)
        try:
            datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as e:
            raise InvalidTimeRangeError(f"unparseable time {t!r}: {e}") from None
        if end and _DATE_ONLY_RE.match(s):
            return s + "T23:59:59.999999"
        return s

    def get(self, ticker: str, signal: str, time_start: Any, time_end: Any,
            as_known_at: Optional[str] = None) -> list[dict]:
        """Retrieve [(event_time, value, ingested_at), ...] for one ticker×signal
        within [time_start, time_end], ascending, PIT-deduped. Raises a
        DataAPIError subclass on an unknown signal/ticker or a bad time range."""
        if signal not in self._signals:
            raise UnknownSignalError(
                f"{signal!r} is not a registered signal; call signals() for the list")
        if not isinstance(ticker, str) or not _TICKER_RE.match(ticker):
            raise UnknownScopeError(f"invalid ticker {ticker!r}")
        ts = self._parse_time(time_start)
        te = self._parse_time(time_end, end=True)
        if ts > te:
            raise InvalidTimeRangeError(f"time_start {ts!r} is after time_end {te!r}")
        if self._conn.execute(
                "SELECT 1 FROM feature_values WHERE feature=? AND scope=? LIMIT 1",
                (signal, ticker)).fetchone() is None:
            raise UnknownScopeError(f"no data for ticker {ticker!r} under signal {signal!r}")
        q = ("SELECT event_time, value, MAX(ingested_at) FROM feature_values "
             "WHERE feature=? AND scope=? AND event_time>=? AND event_time<=?")
        params: list = [signal, ticker, ts, te]
        if as_known_at is not None:
            q += " AND ingested_at<=?"
            params.append(self._parse_time(as_known_at))
        q += " GROUP BY event_time ORDER BY event_time ASC"
        return [{"event_time": et, "value": json.loads(v), "ingested_at": ia}
                for et, v, ia in self._conn.execute(q, params)]

    def latest(self, ticker: str, signal: str, as_of: Optional[str] = None) -> Optional[dict]:
        """Convenience: the single most-recent row at/*before* `as_of` (default now)."""
        end = as_of or _utcnow()
        rows = self.get(ticker, signal, "0001-01-01", end, as_known_at=as_of)
        return rows[-1] if rows else None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
