"""s3_predictors.py — Stage S3: Predictors (DESIGN.md §S3), one file.

The predictive core of the system — separated from Alpha (S4) on purpose:
a PREDICTOR forecasts something about a stock; ALPHA turns forecasts + regime
+ risk into trade decisions. Different concerns, different stages.

This stage is a FRAMEWORK for multiple predictors (more will be added). It
starts with one:

  end_of_day_price  — predicts each stock's forward CLOSE price (and the
                      implied return) at a horizon, from the S1+S2 feature
                      vector. A trained model (Ridge baseline), MEASURED
                      (IC + MSE vs. the predict-the-mean null on a purged,
                      held-out test).

The model EXISTS, TRAINS, and is SCORED daily. The §5 validation gate governs
whether its predictions size REAL CAPITAL downstream — not whether the model
runs. "Don't deploy unvalidated" is not "don't build": a predictor earns the
gate by predicting and being measured every day.

Depends on: S1 (prices) + S2 (features). Provides to: S4 Alpha.
Project style: one file per stage; simple, working. Tests: tests/test_s3_predictors.py.
"""
from __future__ import annotations

from core import Trigger, FeatureStore, MARKET_SCOPE

# ═══════════════ Registry — predictor outputs ═══════════════

S3_FEATURES = [
    ("predict.eod_return", "float", "ticker", "daily",
     "end_of_day_price predictor: predicted forward return over its horizon "
     "(H trading days). OBSERVATION mode — recorded + measured, not sizing "
     "capital until a §5 pass."),
    ("predict.eod_price", "float", "ticker", "daily",
     "end_of_day_price predictor: implied forward close = price.close * "
     "(1 + predicted return)."),
    ("predict.eod_meta", "json", "market", "daily",
     "Per-run predictor metadata: {model, horizon_days, trained_on, "
     "test_metrics, inputs_max_ingested_at}."),
]

# The input feature vector: AS MANY S1/S2 features as are valid model inputs.
# Missing values are mean-imputed (0 after standardization), never sentineled.
# (Raw price/volume LEVELS from S1 are deliberately excluded — non-stationary,
# useless as direct features; they live inside the engineered S2 features here.
# days_to_earnings is excluded from the training panel: yfinance can't give it
# point-in-time for historical dates without lookahead.)
PREDICTOR_FEATURES = [
    # S2 technical
    "tech.rsi14", "tech.mom5", "tech.mom20", "tech.hvol20", "tech.vr20",
    # S2 fundamental (value / quality / size) — from S1 statements/shares/analyst
    "fund.book_to_price", "fund.earnings_yield", "fund.fcf_yield",
    "fund.roe", "fund.gross_profitability", "fund.net_margin", "fund.market_cap",
    # S2 cross-sectional ranks (the selection-relevant form)
    "xsec.rank_rsi14", "xsec.rank_mom5", "xsec.rank_earnings_yield",
    "xsec.rank_fcf_yield", "xsec.rank_roe", "xsec.rank_gross_profitability",
]

EOD_HORIZON_DAYS = 1   # "end of day": next session's close. Configurable.


def register_all(store: FeatureStore) -> None:
    for name, dtype, scope_kind, cadence, pit_rule in S3_FEATURES:
        store.register(name, dtype, scope_kind, source_stage="S3",
                       cadence=cadence, pit_rule=pit_rule)


# ═══════════════ Metrics ═══════════════

def spearman_ic(pred: list[float], actual: list[float]) -> float | None:
    """Rank correlation — the headline predictor-quality metric. None if degenerate."""
    import pandas as pd
    if len(pred) < 3:
        return None
    r = pd.Series(pred).rank().corr(pd.Series(actual).rank())
    return None if r != r else float(r)


# ═══════════════ Training panel (POINT-IN-TIME correct) ═══════════════

def assemble_panel(store: FeatureStore, universe: list[str], dates: list[str],
                   horizon_days: int = EOD_HORIZON_DAYS) -> dict:
    """For each (date, ticker): read the feature vector known AS-OF that date
    (as_known_at=date -> lookahead impossible) and the realized forward return
    over `horizon_days` trading days. Rows lacking a forward price are dropped.
    Returns {X, y, meta, feature_names}."""
    import numpy as np
    X, y, meta = [], [], []
    for d in dates:
        for t in universe:
            # PIT here is enforced by EVENT_TIME (features event_time<=d, forward
            # prices event_time>d) — NOT by ingested_at. When a store is
            # backfilled in one batch every row shares a late ingested_at, so an
            # as_known_at filter would hide everything. The ingested_at/
            # as_known_at guard only bites when real bitemporal CORRECTION
            # history exists; a production backtest with corrections should pass
            # as_known_at per date. Backfill uses event_time bounds only.
            p0 = store.read_asof("price.close", t, d)
            if not p0 or p0["event_time"] != d:
                continue
            series = store.read_series("price.close", t, "2099-01-01", 999999)
            future = [v for et, v in series if et > d]
            if len(future) < horizon_days:
                continue
            fwd_ret = future[horizon_days - 1] / p0["value"] - 1.0
            feats = [(store.read_asof(f, t, d) or {}).get("value", np.nan)
                     for f in PREDICTOR_FEATURES]
            X.append(feats)
            y.append(fwd_ret)
            meta.append((d, t))
    return {"X": np.array(X, dtype=float), "y": np.array(y, dtype=float),
            "meta": meta, "feature_names": list(PREDICTOR_FEATURES)}


def train(X, y, alpha: float = 10.0) -> dict:
    """Ridge baseline: linear + regularized (won't overfit small/noisy data),
    coefficients ARE factor loadings (interpretable). Standardizes features,
    mean-imputes missing."""
    import numpy as np
    from sklearn.linear_model import Ridge
    mean = np.nanmean(X, axis=0)
    std = np.nanstd(X, axis=0)
    std[std == 0] = 1.0
    Xz = np.where(np.isnan(X), 0.0, (X - mean) / std)
    model = Ridge(alpha=alpha).fit(Xz, y)
    return {"model": model, "mean": mean, "std": std, "horizon_days": None,
            "feature_names": list(PREDICTOR_FEATURES),
            "coefficients": dict(zip(PREDICTOR_FEATURES, model.coef_.tolist()))}


def _predict_vec(trained, X):
    import numpy as np
    Xz = np.where(np.isnan(X), 0.0, (X - trained["mean"]) / trained["std"])
    return trained["model"].predict(Xz)


def evaluate(trained, X, y) -> dict:
    """Held-out RETURN metrics — the cross-sectional view. IC (rank corr) +
    MSE vs. the predict-the-mean null. A model that doesn't beat the null
    doesn't ship."""
    import numpy as np
    pred = _predict_vec(trained, X)
    mse = float(np.mean((pred - y) ** 2))
    null_mse = float(np.mean((y - np.mean(y)) ** 2))
    return {"n": int(len(y)), "ic": spearman_ic(pred.tolist(), y.tolist()),
            "mse": mse, "rmse": float(mse ** 0.5),
            "null_mse": null_mse, "null_rmse": float(null_mse ** 0.5),
            "r2_vs_null": (1 - mse / null_mse) if null_mse else None,
            "beats_null": bool(mse < null_mse)}


def evaluate_price(pred_returns, actual_returns, base_prices) -> dict:
    """PRICE-prediction view — what a price predictor is actually judged on.
    predicted_price = base * (1 + pred_return); actual = base * (1 + actual);
    naive persistence = base (tomorrow == today). Reports RMSE/MAE/MAPE for the
    MODEL and the NAIVE baseline, and whether the model beats naive.

    This baseline is non-negotiable: on a near-random-walk series, naive
    persistence gives tiny error that looks like skill. A price model that does
    not beat naive has learned nothing, however pretty its predicted-vs-actual
    chart. (machinelearningmastery.com random-walk forecasting.)"""
    import numpy as np
    base = np.asarray(base_prices, float)
    actual_px = base * (1 + np.asarray(actual_returns, float))
    model_px = base * (1 + np.asarray(pred_returns, float))
    naive_px = base                                        # persistence

    def metrics(px):
        err = px - actual_px
        return {"rmse": float(np.sqrt(np.mean(err ** 2))),
                "mae": float(np.mean(np.abs(err))),
                "mape_pct": float(np.mean(np.abs(err / actual_px)) * 100)}

    m, nv = metrics(model_px), metrics(naive_px)
    return {"n": int(len(base)), "model": m, "naive_persistence": nv,
            "model_beats_naive_rmse": bool(m["rmse"] < nv["rmse"]),
            "rmse_improvement_pct": float((nv["rmse"] - m["rmse"]) / nv["rmse"] * 100)}


# ═══════════════ Live prediction + orchestrator ═══════════════

def predict_eod(store: FeatureStore, universe: list[str], event_date: str,
                trained, horizon_days: int = EOD_HORIZON_DAYS,
                as_known_at: str | None = None) -> dict:
    """Predict each ticker's forward return + implied close from today's
    features. Returns {ticker: {eod_return, eod_price}}."""
    import numpy as np
    out = {}
    for t in universe:
        feats = [(store.read_asof(f, t, event_date, as_known_at) or {}).get("value", np.nan)
                 for f in PREDICTOR_FEATURES]
        r = float(_predict_vec(trained, np.array([feats], dtype=float))[0])
        px = store.read_asof("price.close", t, event_date, as_known_at)
        out[t] = {"eod_return": round(r, 6),
                  "eod_price": round(px["value"] * (1 + r), 4) if px else None}
    return out


def run_predictors(universe: list[str], event_date: str,
                   store: FeatureStore | None = None, trained=None,
                   horizon_days: int = EOD_HORIZON_DAYS,
                   as_known_at: str | None = None) -> dict:
    """One predictor pass. Without a trained model -> status NO_MODEL (the
    model must be trained by train() on an assembled panel first). With one ->
    writes predict.eod_return / predict.eod_price per ticker (observation mode)."""
    store = store or FeatureStore()
    register_all(store)

    with Trigger("predictors", stage="S3") as trig:
        if trained is None:
            trig.add_metrics(event_date=event_date, universe_size=len(universe),
                             status="NO_MODEL")
            return {"trigger_id": trig.trigger_id, **trig.metrics}

        preds = predict_eod(store, universe, event_date, trained, horizon_days, as_known_at)
        written = 0
        for t, p in preds.items():
            store.write("predict.eod_return", t, event_date, p["eod_return"],
                        trigger_id=trig.trigger_id)
            if p["eod_price"] is not None:
                store.write("predict.eod_price", t, event_date, p["eod_price"],
                            trigger_id=trig.trigger_id)
            written += 1
        store.write("predict.eod_meta", MARKET_SCOPE, event_date,
                    {"model": "end_of_day_price/ridge", "horizon_days": horizon_days,
                     "test_metrics": trained.get("test_metrics")},
                    trigger_id=trig.trigger_id)
        trig.add_metrics(event_date=event_date, universe_size=len(universe),
                         status="PREDICTED", predictions_written=written,
                         horizon_days=horizon_days)
        return {"trigger_id": trig.trigger_id, **trig.metrics}
