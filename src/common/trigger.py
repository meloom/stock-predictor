"""Trigger context: the unit of cost attribution and run logging.

Every pipeline invocation starts from a trigger (cron fire, review cycle,
report run, retrospective, manual invocation). The trigger mints a trigger_id
that must be carried by every billable action and every feature write it
transitively initiates — this is what makes S8's cost-per-trigger metric and
stale-input lineage audit possible (docs/DESIGN.md, cross-cutting metric).

Two append-only JSONL logs, both under the gitignored runtime dir:
  runtime/logs/runs.jsonl        one record per trigger run (status + metrics)
  runtime/logs/cost_ledger.jsonl one record per billable action

Design rules encoded here:
  - Append-only: nothing here ever rewrites history.
  - A trigger that crashes still logs a run record (status="error") — silence
    is indistinguishable from success otherwise, and the predecessor had
    failures that were only discovered days later for exactly that reason.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Repo-relative runtime dir unless overridden; gitignored, never committed.
RUNTIME_DIR = Path(os.environ.get(
    "STOCK_PREDICTOR_RUNTIME",
    Path(__file__).resolve().parents[2] / "runtime",
))
LOGS_DIR = RUNTIME_DIR / "logs"

# Estimated unit prices, USD. These are for the *relative* cost-per-trigger
# signal (regression detection), not accounting-grade billing — the billing
# dashboard remains ground truth for absolute spend.
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
