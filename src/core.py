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
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
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
