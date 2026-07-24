"""s3_alpha.py — Stage S3: alpha (DESIGN.md §S3), one file.

Three components, per the design:
  A1 regime gate    — IMPLEMENTED as a transparent rule-based v1 (below).
  A2 stock scorer   — DISABLED BY DESIGN. The predecessor's technical ranker
                      measured Spearman IC ≈ 0 vs. excess returns on purged
                      OOS data and its live picks underperformed its own
                      avoid-list two days running. score_stocks() returns an
                      explicit DISABLED status until a scorer passes §5.
  A3 event risk     — deterministic, from calendar.days_to_earnings only.

Why the regime gate is rule-based here (not the predecessor's 5-model ML
ensemble): those models are unreviewable pickled binaries excluded from this
repo by policy, trained by the archived iteration stack. v1 is three
transparent components — breadth, VIX, SPY trend — deterministic and fully
testable. It is UNVALIDATED (§5) and therefore informational-only; nothing
can trade off it anyway while the scorer is disabled, and on missing inputs
it fails TOWARD CASH, never toward trading.

Every decision record carries a data-lineage stamp: the max ingested_at of
every store value used to compute it. A decision computed from stale inputs
is a pipeline defect, not a model error — the stamp is what lets S8 tell the
difference (predecessor failure: 22 consecutive regime cycles on a
pre-market placeholder value, undetected).

Project style: one file per stage; simple, working. Tests: tests/test_s3_alpha.py.
"""
from __future__ import annotations

from core import Trigger, FeatureStore, MARKET_SCOPE

# ═══════════════ Registry — the S3 decision records ═══════════════

S3_FEATURES = [
    ("alpha.regime", "json", "market", "daily",
     "Regime decision for the day, computed from features with event_time <= "
     "the day. Value: {score, decision, components, inputs_max_ingested_at}."),
    ("alpha.event_risk", "json", "ticker", "daily",
     "Deterministic earnings-proximity risk. Value: {level, days_to_earnings, "
     "inputs_max_ingested_at}. UNKNOWN when the calendar has no data — never "
     "silently LOW."),
]

REGIME_THRESHOLD = 0.6  # score >= threshold -> "TRADE", else "CASH" (informational)


def register_all(store: FeatureStore) -> None:
    for name, dtype, scope_kind, cadence, pit_rule in S3_FEATURES:
        store.register(name, dtype, scope_kind, source_stage="S3",
                       cadence=cadence, pit_rule=pit_rule)


# ═══════════════ A1: rule-based regime gate v1 ═══════════════

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def compute_regime(store: FeatureStore, event_date: str,
                   as_known_at: str | None = None) -> dict:
    """Transparent 3-component score in [0,1]:
        breadth   (weight .4): regime.breadth5 as-is (fraction of universe up)
        vix       (weight .3): 1.0 at VIX<=15, 0.0 at VIX>=30, linear between
        spy_trend (weight .3): SPY last close vs. its 20-day mean, +/-2% band
    Missing ANY component -> decision CASH with the missing inputs named.
    Fail-safe direction is always cash.
    """
    inputs: dict[str, dict | None] = {
        "breadth5": store.read_asof("regime.breadth5", MARKET_SCOPE, event_date, as_known_at),
        "vix": store.read_asof("macro.vix", MARKET_SCOPE, event_date, as_known_at),
    }
    spy_series = store.read_series("macro.spy_close", MARKET_SCOPE, event_date, 20, as_known_at)

    missing = [k for k, v in inputs.items() if v is None]
    if len(spy_series) < 20:
        missing.append(f"spy_close({len(spy_series)}/20 days)")
    if missing:
        return {"score": None, "decision": "CASH",
                "reason": f"missing inputs: {', '.join(missing)} — failing toward cash",
                "components": {}, "inputs_max_ingested_at": None}

    breadth = inputs["breadth5"]["value"]
    vix = inputs["vix"]["value"]
    spy_closes = [v for _, v in spy_series]
    sma20 = sum(spy_closes) / len(spy_closes)
    trend_dev = spy_closes[-1] / sma20 - 1.0

    components = {
        "breadth": _clamp(breadth),
        "vix": _clamp((30.0 - vix) / 15.0),
        "spy_trend": _clamp((trend_dev + 0.02) / 0.04),
    }
    score = round(0.4 * components["breadth"] + 0.3 * components["vix"]
                  + 0.3 * components["spy_trend"], 4)
    lineage = max(inputs["breadth5"]["ingested_at"], inputs["vix"]["ingested_at"])
    return {"score": score,
            "decision": "TRADE" if score >= REGIME_THRESHOLD else "CASH",
            "reason": f"score {score} vs threshold {REGIME_THRESHOLD}",
            "components": {k: round(v, 4) for k, v in components.items()},
            "inputs_max_ingested_at": lineage}


# ═══════════════ A2: stock scorer — DISABLED BY DESIGN ═══════════════

SCORER_STATUS = {
    "status": "DISABLED",
    "reason": ("No scorer has passed the §5 validation gate. The predecessor's "
               "technical ranker measured IC ~ 0 on purged OOS data and its live "
               "picks underperformed its own avoid-list; it was not migrated. "
               "Rebuild around fundamentals and validate before enabling."),
}


def score_stocks(*_args, **_kwargs) -> dict:
    """Explicitly disabled — returns status, never a ranking. Enabling this
    requires a §5 pass and a design-doc status change in the same commit."""
    return dict(SCORER_STATUS)


# ═══════════════ A3: deterministic event risk ═══════════════

def classify_event_risk(days_to_earnings: int | None) -> str:
    """Deterministic only. UNKNOWN when the calendar is silent — the
    predecessor's 999-sentinel effectively mapped 'unknown' to 'LOW', which
    is a claim ('no event near') the data doesn't support."""
    if days_to_earnings is None:
        return "UNKNOWN"
    if days_to_earnings <= 2:
        return "HIGH"
    if days_to_earnings <= 5:
        return "MEDIUM"
    return "LOW"


# ═══════════════ Orchestrator ═══════════════

def run_alpha(universe: list[str], event_date: str,
              store: FeatureStore | None = None,
              as_known_at: str | None = None) -> dict:
    store = store or FeatureStore()
    register_all(store)

    with Trigger("alpha", stage="S3") as trig:
        regime = compute_regime(store, event_date, as_known_at)
        store.write("alpha.regime", MARKET_SCOPE, event_date, regime,
                    trigger_id=trig.trigger_id)

        risk_counts: dict[str, int] = {}
        for t in universe:
            rec = store.read_asof("calendar.days_to_earnings", t, event_date, as_known_at)
            dte = rec["value"] if rec else None
            level = classify_event_risk(dte)
            risk_counts[level] = risk_counts.get(level, 0) + 1
            store.write("alpha.event_risk", t, event_date,
                        {"level": level, "days_to_earnings": dte,
                         "inputs_max_ingested_at": rec["ingested_at"] if rec else None},
                        trigger_id=trig.trigger_id)

        trig.add_metrics(
            event_date=event_date,
            universe_size=len(universe),
            regime_decision=regime["decision"],
            regime_score=regime["score"],
            regime_reason=regime["reason"],
            event_risk_counts=risk_counts,
            scorer=SCORER_STATUS["status"],
        )
        return {"trigger_id": trig.trigger_id, **trig.metrics}
