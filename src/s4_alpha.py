"""s4_alpha.py — Stage S4: Alpha (DESIGN.md §S4), one file.

Alpha turns PREDICTIONS (from S3 Predictors) + regime + event risk into a
per-stock trade signal. It does NOT predict — that's S3's job. Separation of
concerns: S3 forecasts, S4 decides.

Components:
  regime gate  — transparent rule-based v1 (breadth + VIX + SPY trend). On
                 missing inputs, fails TOWARD CASH. Unvalidated (§5) =>
                 informational.
  event risk   — deterministic from calendar.days_to_earnings; missing = UNKNOWN,
                 never silently LOW.
  combine      — reads S3's predict.ret_1d per ticker and gates it: no
                 trade if regime is CASH; HIGH event risk vetoes a name. Output
                 is an alpha signal per stock, OBSERVATION mode until the
                 upstream predictor passes §5.

Every decision record carries a data-lineage stamp (max ingested_at of inputs)
so S8/S9 can tell a stale-input defect from a model error.

Depends on: S1, S2, S3. Provides to: S5 Portfolio.
Project style: one file per stage; simple, working. Tests: tests/test_s4_alpha.py.
"""
from __future__ import annotations

from core import Trigger, FeatureStore, MARKET_SCOPE

# ═══════════════ Registry — S4 decision records ═══════════════

S4_FEATURES = [
    ("alpha.regime", "json", "market", "daily",
     "Regime decision: {score, decision, components, inputs_max_ingested_at}."),
    ("alpha.event_risk", "json", "ticker", "daily",
     "Deterministic earnings-proximity risk {level, days_to_earnings, "
     "inputs_max_ingested_at}. UNKNOWN when the calendar is silent, never LOW."),
    ("alpha.signal", "json", "ticker", "daily",
     "Per-stock alpha signal: {predicted_return, regime_ok, event_risk, "
     "actionable}. Combines S3's prediction with the regime gate + event veto. "
     "OBSERVATION mode — does not size capital until the predictor passes §5."),
]

REGIME_THRESHOLD = 0.6


def register_all(store: FeatureStore) -> None:
    for name, dtype, scope_kind, cadence, pit_rule in S4_FEATURES:
        store.register(name, dtype, scope_kind, source_stage="S4",
                       cadence=cadence, pit_rule=pit_rule)


# ═══════════════ Regime gate (rule-based v1) ═══════════════

def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def compute_regime(store: FeatureStore, event_date: str,
                   as_known_at: str | None = None) -> dict:
    """Transparent 3-component score in [0,1]: breadth(.4) + VIX(.3) +
    SPY-20d-trend(.3). Missing any input -> CASH, naming the gap. Fail-safe
    direction is always cash."""
    inputs = {
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
    breadth, vix = inputs["breadth5"]["value"], inputs["vix"]["value"]
    spy_closes = [v for _, v in spy_series]
    trend_dev = spy_closes[-1] / (sum(spy_closes) / len(spy_closes)) - 1.0
    components = {"breadth": _clamp(breadth), "vix": _clamp((30.0 - vix) / 15.0),
                  "spy_trend": _clamp((trend_dev + 0.02) / 0.04)}
    score = round(0.4 * components["breadth"] + 0.3 * components["vix"]
                  + 0.3 * components["spy_trend"], 4)
    return {"score": score,
            "decision": "TRADE" if score >= REGIME_THRESHOLD else "CASH",
            "reason": f"score {score} vs threshold {REGIME_THRESHOLD}",
            "components": {k: round(v, 4) for k, v in components.items()},
            "inputs_max_ingested_at": max(inputs["breadth5"]["ingested_at"],
                                          inputs["vix"]["ingested_at"])}


# ═══════════════ Event risk (deterministic) ═══════════════

def classify_event_risk(days_to_earnings: int | None) -> str:
    if days_to_earnings is None:
        return "UNKNOWN"
    if days_to_earnings <= 2:
        return "HIGH"
    if days_to_earnings <= 5:
        return "MEDIUM"
    return "LOW"


# ═══════════════ Orchestrator: combine prediction + regime + event risk ═══════════════

def run_alpha(universe: list[str], event_date: str,
              store: FeatureStore | None = None,
              as_known_at: str | None = None) -> dict:
    store = store or FeatureStore()
    register_all(store)

    with Trigger("alpha", stage="S4") as trig:
        regime = compute_regime(store, event_date, as_known_at)
        store.write("alpha.regime", MARKET_SCOPE, event_date, regime,
                    trigger_id=trig.trigger_id)
        regime_ok = regime["decision"] == "TRADE"

        risk_counts, actionable = {}, 0
        for t in universe:
            rec = store.read_asof("calendar.days_to_earnings", t, event_date, as_known_at)
            dte = rec["value"] if rec else None
            level = classify_event_risk(dte)
            risk_counts[level] = risk_counts.get(level, 0) + 1
            store.write("alpha.event_risk", t, event_date,
                        {"level": level, "days_to_earnings": dte,
                         "inputs_max_ingested_at": rec["ingested_at"] if rec else None},
                        trigger_id=trig.trigger_id)

            # combine S3's prediction with the gate + event veto
            pred = store.read_asof("predict.ret_1d", t, event_date, as_known_at)
            pred_ret = pred["value"] if pred else None
            is_actionable = bool(regime_ok and level != "HIGH" and pred_ret is not None
                                 and pred_ret > 0)
            if is_actionable:
                actionable += 1
            store.write("alpha.signal", t, event_date,
                        {"predicted_return": pred_ret, "regime_ok": regime_ok,
                         "event_risk": level, "actionable": is_actionable,
                         "inputs_max_ingested_at": pred["ingested_at"] if pred else None},
                        trigger_id=trig.trigger_id)

        trig.add_metrics(
            event_date=event_date, universe_size=len(universe),
            regime_decision=regime["decision"], regime_score=regime["score"],
            event_risk_counts=risk_counts,
            has_predictions=bool(universe and store.read_asof(
                "predict.ret_1d", universe[0], event_date)),
            actionable_signals=actionable)
        return {"trigger_id": trig.trigger_id, **trig.metrics}
