"""Feature store: registry + bitemporal storage + the one read API.

Implements the contract in docs/DESIGN.md §S1:
  - Registry-first: writes to unregistered features are rejected.
  - Bitemporal keys (feature, scope, event_time, ingested_at):
      * event_time  = when the value is *about* (backtests join on this —
                      lookahead-impossible by construction via as_known_at)
      * ingested_at = when we learned it (S8's stale-input audit checks this)
  - Append-only: corrections append a new ingested_at version; history is
    never rewritten, so any past prediction is exactly reproducible.
  - One read API for research and production — no separate code paths.

Storage is SQLite (stdlib, single file, zero infra). The contract is the
point; the backend is deliberately boring.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

RUNTIME_DIR = Path(os.environ.get(
    "STOCK_PREDICTOR_RUNTIME",
    Path(__file__).resolve().parents[2] / "runtime",
))
DEFAULT_DB = RUNTIME_DIR / "features.db"

MARKET_SCOPE = "_market"  # scope value for market-level (non-per-ticker) features

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
