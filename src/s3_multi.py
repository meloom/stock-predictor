"""s3_multi.py — multi-horizon predictors, walk-forward OVER TIME + a forward FORECAST.

Three predictor families, each at horizons 1 / 5 / 21 trading days:
  • return      — forward return           (Ridge regression)  -> predict.ret_h{H}
  • direction   — P(up) over the horizon    (logistic)          -> predict.up_h{H}
  • volatility  — forward realized vol       (Ridge, h=5)        -> predict.vol_h5

Leakage-free by construction:
  • OVER TIME  — expanding-window walk-forward: each prediction block is scored by a
    model trained ONLY on dates strictly before the block (never on its own future).
  • INTO THE FUTURE — a final model trained on all labeled history predicts the LATEST
    date, whose forward label doesn't exist yet -> a genuine forward forecast, stored as
    predict.forecast = {H: {target_date_offset, pred_return, pred_price}} + the live
    predict.ret_h{H} value at the latest date.

Inputs: the S1+S2 feature vector (s3_predictors.PREDICTOR_FEATURES). Provides to: S4.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import FeatureStore, DEFAULT_DB, MARKET_SCOPE, Trigger      # noqa: E402
from s3_predictors import PREDICTOR_FEATURES                          # noqa: E402

HORIZONS = [1, 5, 21]
VOL_HORIZON = 5
WARMUP_FRAC = 0.4          # first 40% of dates are the initial training pool
RETRAIN_EVERY = 21        # retrain (expanding window) every ~month of trading days

S3M_FEATURES = []
for _h in HORIZONS:
    S3M_FEATURES += [
        (f"predict.ret_h{_h}", "float", "ticker", "daily",
         f"Predicted forward return over {_h} trading day(s) (Ridge, walk-forward OOS)."),
        (f"predict.up_h{_h}", "float", "ticker", "daily",
         f"Predicted P(up) over {_h} trading day(s) (logistic, walk-forward OOS)."),
    ]
S3M_FEATURES += [
    (f"predict.vol_h{VOL_HORIZON}", "float", "ticker", "daily",
     f"Predicted forward realized volatility over {VOL_HORIZON} trading days (Ridge)."),
    ("predict.forecast", "json", "ticker", "daily",
     "Forward-dated forecast per horizon: {H: {ahead, pred_return, pred_price}}."),
]


def register_all(store: FeatureStore) -> None:
    import s3_predictors
    s3_predictors.register_all(store)            # predict.eod_return/eod_price/… base outputs
    for name, dtype, sk, cadence, rule in S3M_FEATURES:
        store.register(name, dtype, sk, source_stage="S3", cadence=cadence, pit_rule=rule)


# ── load the panel efficiently (one sweep of feature_values, not per-row reads) ──
def _load(db_path):
    con = sqlite3.connect(Path(db_path))
    cols = list(PREDICTOR_FEATURES)
    idx = {f: i for i, f in enumerate(cols)}
    feats, price = {}, {}
    ph = ",".join("?" * len(cols))
    q = (f"SELECT feature, scope, event_time, value FROM feature_values "
         f"WHERE feature IN ({ph}) OR feature='price.close' ORDER BY ingested_at")
    for feature, scope, et, val in con.execute(q, cols):
        if feature == "price.close":
            try:
                price.setdefault(scope, {})[et] = float(val)
            except (TypeError, ValueError):
                pass
            continue
        try:
            v = float(val)
        except (TypeError, ValueError):
            v = float("nan")
        feats.setdefault((scope, et), [float("nan")] * len(cols))[idx[feature]] = v
    return feats, price, cols


def _panel(db_path):
    """Rows of (date, ticker, x, labels) where labels = forward ret/up per horizon +
    forward realized vol. Labels are None when the forward window runs past history."""
    import numpy as np
    feats, price, cols = _load(db_path)
    rows = []
    latest_date = max((d for pm in price.values() for d in pm), default=None)
    for t, pm in price.items():
        ds = sorted(pm)
        closes = [pm[d] for d in ds]
        drets = [closes[i + 1] / closes[i] - 1 for i in range(len(closes) - 1)
                 if closes[i]]
        for i, d in enumerate(ds):
            x = feats.get((t, d))
            if x is None:
                continue
            lab = {}
            for h in HORIZONS:
                lab[f"ret_h{h}"] = (closes[i + h] / closes[i] - 1
                                    if i + h < len(closes) and closes[i] else None)
                lab[f"up_h{h}"] = (1.0 if (lab[f"ret_h{h}"] or 0) > 0 else 0.0) \
                    if lab[f"ret_h{h}"] is not None else None
            vw = drets[i:i + VOL_HORIZON]
            lab[f"vol_h{VOL_HORIZON}"] = (float(np.std(vw)) if len(vw) == VOL_HORIZON else None)
            rows.append({"d": d, "t": t, "x": x, "lab": lab, "close": closes[i]})
    return rows, cols, latest_date


def _fit(X, y, kind):
    import numpy as np
    with np.errstate(all="ignore"):
        mean = np.nan_to_num(np.nanmean(X, axis=0), nan=0.0)     # all-NaN col -> 0
        std = np.nan_to_num(np.nanstd(X, axis=0), nan=1.0); std[std == 0] = 1.0
    Xz = np.nan_to_num((np.nan_to_num(X, nan=0.0) - mean) / std, nan=0.0,
                       posinf=0.0, neginf=0.0)
    if kind == "logistic":
        from sklearn.linear_model import LogisticRegression
        if len(set(y.tolist())) < 2:
            return None
        m = LogisticRegression(max_iter=500, C=0.5).fit(Xz, y)
    else:
        from sklearn.linear_model import Ridge
        m = Ridge(alpha=10.0).fit(Xz, y)
    return {"m": m, "mean": mean, "std": std, "kind": kind}


def _pred(model, X):
    import numpy as np
    Xz = np.nan_to_num((np.nan_to_num(X, nan=0.0) - model["mean"]) / model["std"],
                       nan=0.0, posinf=0.0, neginf=0.0)
    if model["kind"] == "logistic":
        return model["m"].predict_proba(Xz)[:, 1]
    return model["m"].predict(Xz)


def _walk_forward(rows, cols, label, kind):
    """Expanding-window OOS predictions {(t,d): value}. Trains only on dates strictly
    before each prediction block."""
    import numpy as np
    dates = sorted({r["d"] for r in rows})
    if len(dates) < 20:
        return {}
    start = int(len(dates) * WARMUP_FRAC)
    out = {}
    for b in range(start, len(dates), RETRAIN_EVERY):
        block = set(dates[b:b + RETRAIN_EVERY])
        cutoff = dates[b]
        tr = [r for r in rows if r["d"] < cutoff and r["lab"].get(label) is not None]
        if len(tr) < 100:
            continue
        model = _fit(np.array([r["x"] for r in tr], dtype=float),
                     np.array([r["lab"][label] for r in tr], dtype=float), kind)
        if model is None:
            continue
        pb = [r for r in rows if r["d"] in block]
        if not pb:
            continue
        preds = _pred(model, np.array([r["x"] for r in pb], dtype=float))
        for r, p in zip(pb, preds):
            out[(r["t"], r["d"])] = float(p)
    return out


def backfill(store: FeatureStore | None = None, db_path=DEFAULT_DB) -> dict:
    """Write the walk-forward OOS prediction time series for every predictor, PLUS the
    forward forecast at the latest date."""
    import numpy as np
    store = store or FeatureStore()
    register_all(store)
    rows, cols, latest = _panel(db_path)
    written = 0
    with Trigger("predictors_multi", stage="S3") as trig:
        # over-time: walk-forward OOS for each predictor
        targets = ([(f"ret_h{h}", "ridge") for h in HORIZONS]
                   + [(f"up_h{h}", "logistic") for h in HORIZONS]
                   + [(f"vol_h{VOL_HORIZON}", "ridge")])
        for label, kind in targets:
            for (t, d), val in _walk_forward(rows, cols, label, kind).items():
                store.write(f"predict.{label}", t, d, round(val, 6), trigger_id=trig.trigger_id)
                written += 1
        # S4 compatibility: predict.eod_return == the 1-day return predictor
        for (t, d), val in _walk_forward(rows, cols, "ret_h1", "ridge").items():
            store.write("predict.eod_return", t, d, round(val, 6), trigger_id=trig.trigger_id)

        # into the future: final model on ALL labeled history -> predict the latest date
        forecasts = {}
        for label, kind in targets:
            tr = [r for r in rows if r["lab"].get(label) is not None]
            if len(tr) < 100:
                continue
            model = _fit(np.array([r["x"] for r in tr], dtype=float),
                         np.array([r["lab"][label] for r in tr], dtype=float), kind)
            if model is None:
                continue
            live = [r for r in rows if r["d"] == latest]
            if not live:
                continue
            preds = _pred(model, np.array([r["x"] for r in live], dtype=float))
            for r, p in zip(live, preds):
                forecasts.setdefault(r["t"], {"close": r["close"]})[label] = float(p)
        fc_written = 0
        for t, fc in forecasts.items():
            close = fc.get("close")
            doc = {}
            for h in HORIZONS:
                ret = fc.get(f"ret_h{h}")
                if ret is None:
                    continue
                doc[str(h)] = {"ahead": f"+{h} trading days",
                               "pred_return": round(ret, 6),
                               "pred_price": round(close * (1 + ret), 4) if close else None,
                               "p_up": round(fc.get(f"up_h{h}"), 4) if fc.get(f"up_h{h}") is not None else None}
            if doc:
                store.write("predict.forecast", t, latest, doc, trigger_id=trig.trigger_id)
                fc_written += 1
        trig.add_metrics(status="DONE", oos_predictions=written, latest_date=latest,
                         forecasts=fc_written, horizons=HORIZONS)
        return {"trigger_id": trig.trigger_id, **trig.metrics}


if __name__ == "__main__":
    from universe import UNIVERSE  # noqa
    print(json.dumps(backfill(), default=str, indent=2))
