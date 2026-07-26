"""s3_multi.py — multi-horizon predictors, walk-forward OVER TIME + a forward FORECAST.

Naming: outputs are `<family>_<N>d` (e.g. ret_1d, up_5d, down_21d, vol_5d) — the horizon
is N trading days.

Three predictor families, each at horizons 1 / 5 / 21 trading days:
  • return     — forward return with a 95% CONFIDENCE INTERVAL (Ridge; CI = pred ± 1.96σ,
                 σ = training-residual std)          -> predict.ret_<N>d  (+ predict.band σ)
  • direction  — 3-class up/flat/down (multinomial)  -> predict.up_<N>d, predict.down_<N>d
  • volatility — forward realized vol (Ridge, h=5)    -> predict.vol_5d

Leakage-free:
  • OVER TIME  — expanding-window walk-forward: each block scored by a model trained only
    on strictly-earlier dates.
  • INTO THE FUTURE — a final model trained on all labeled history predicts the LATEST
    date -> predict.forecast per ticker {N: {ahead, pred_return, ci_low, ci_high,
    pred_price, p_up, p_down}}.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import FeatureStore, DEFAULT_DB, MARKET_SCOPE, Trigger      # noqa: E402
from s3_predictors import PREDICTOR_FEATURES                          # noqa: E402

HORIZONS = [1, 5, 21]
VOL_HORIZON = 5
WARMUP_FRAC = 0.4
RETRAIN_EVERY = 21
Z95 = 1.96


def _thr(h):
    """Directional deadband for horizon h (~0.5% × √h): 'up' = move beyond +thr."""
    return round(0.005 * math.sqrt(h), 4)


S3M_FEATURES = []
for _h in HORIZONS:
    S3M_FEATURES += [
        (f"predict.ret_{_h}d", "float", "ticker", "daily",
         f"Predicted forward return over {_h} trading day(s) (Ridge, 95% CI via predict.band)."),
        (f"predict.up_{_h}d", "float", "ticker", "daily",
         f"P(up move > +{_thr(_h) * 100:.1f}%) over {_h}d (3-class direction)."),
        (f"predict.down_{_h}d", "float", "ticker", "daily",
         f"P(down move < -{_thr(_h) * 100:.1f}%) over {_h}d (3-class direction)."),
    ]
S3M_FEATURES += [
    (f"predict.vol_{VOL_HORIZON}d", "float", "ticker", "daily",
     f"Predicted forward realized volatility over {VOL_HORIZON} trading days (Ridge)."),
    ("predict.forecast", "json", "ticker", "daily",
     "Forward-dated forecast per horizon incl. 95% confidence interval."),
    ("predict.band", "json", "market", "daily",
     "Per-horizon return residual σ — the width of the return confidence interval."),
]


def register_all(store: FeatureStore) -> None:
    import s3_predictors
    s3_predictors.register_all(store)
    for name, dtype, sk, cadence, rule in S3M_FEATURES:
        store.register(name, dtype, sk, source_stage="S3", cadence=cadence, pit_rule=rule)


# ── load the panel efficiently (one sweep of feature_values) ──
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
    import numpy as np
    feats, price, cols = _load(db_path)
    rows = []
    latest_date = max((d for pm in price.values() for d in pm), default=None)
    for t, pm in price.items():
        ds = sorted(pm)
        closes = [pm[d] for d in ds]
        drets = [closes[i + 1] / closes[i] - 1 for i in range(len(closes) - 1) if closes[i]]
        for i, d in enumerate(ds):
            x = feats.get((t, d))
            if x is None:
                continue
            lab = {}
            for h in HORIZONS:
                fwd = (closes[i + h] / closes[i] - 1
                       if i + h < len(closes) and closes[i] else None)
                lab[f"ret_{h}d"] = fwd
                thr = _thr(h)
                lab[f"dir_{h}d"] = (None if fwd is None else
                                    (1 if fwd > thr else (-1 if fwd < -thr else 0)))
            vw = drets[i:i + VOL_HORIZON]
            lab[f"vol_{VOL_HORIZON}d"] = float(np.std(vw)) if len(vw) == VOL_HORIZON else None
            rows.append({"d": d, "t": t, "x": x, "lab": lab, "close": closes[i]})
    return rows, cols, latest_date


# ── fit / predict ──
def _stdz(X, mean, std):
    import numpy as np
    return np.nan_to_num((np.nan_to_num(X, nan=0.0) - mean) / std, nan=0.0,
                         posinf=0.0, neginf=0.0)


def _norm(X):
    import numpy as np
    with np.errstate(all="ignore"):
        mean = np.nan_to_num(np.nanmean(X, axis=0), nan=0.0)
        std = np.nan_to_num(np.nanstd(X, axis=0), nan=1.0); std[std == 0] = 1.0
    return mean, std


def _fit_reg(X, y):
    """Ridge + residual σ (for the confidence interval)."""
    import numpy as np
    from sklearn.linear_model import Ridge
    mean, std = _norm(X); Xz = _stdz(X, mean, std)
    m = Ridge(alpha=10.0).fit(Xz, y)
    sigma = float(np.std(y - m.predict(Xz))) if len(y) > 2 else 0.0
    return {"m": m, "mean": mean, "std": std, "kind": "reg", "sigma": sigma}


def _fit_clf(X, y):
    """Multinomial logistic over {-1,0,1} directional classes."""
    from sklearn.linear_model import LogisticRegression
    if len(set(y.tolist())) < 2:
        return None
    mean, std = _norm(X)
    m = LogisticRegression(max_iter=500, C=0.5).fit(_stdz(X, mean, std), y)
    return {"m": m, "mean": mean, "std": std, "kind": "clf",
            "classes": list(m.classes_)}


def _pred_reg(model, X):
    return model["m"].predict(_stdz(X, model["mean"], model["std"]))


def _pred_proba(model, X):
    return model["m"].predict_proba(_stdz(X, model["mean"], model["std"]))


def _blocks(dates):
    start = int(len(dates) * WARMUP_FRAC)
    for b in range(start, len(dates), RETRAIN_EVERY):
        yield dates[b], set(dates[b:b + RETRAIN_EVERY])


def _walk_reg(rows, label):
    import numpy as np
    dates = sorted({r["d"] for r in rows}); out = {}
    if len(dates) < 20:
        return out
    for cutoff, block in _blocks(dates):
        tr = [r for r in rows if r["d"] < cutoff and r["lab"].get(label) is not None]
        if len(tr) < 100:
            continue
        m = _fit_reg(np.array([r["x"] for r in tr], dtype=float),
                     np.array([r["lab"][label] for r in tr], dtype=float))
        pb = [r for r in rows if r["d"] in block]
        if not pb:
            continue
        for r, p in zip(pb, _pred_reg(m, np.array([r["x"] for r in pb], dtype=float))):
            out[(r["t"], r["d"])] = float(p)
    return out


def _walk_dir(rows, h):
    """Walk-forward 3-class direction -> (p_up, p_down) maps."""
    import numpy as np
    label = f"dir_{h}d"
    dates = sorted({r["d"] for r in rows}); up, down = {}, {}
    if len(dates) < 20:
        return up, down
    for cutoff, block in _blocks(dates):
        tr = [r for r in rows if r["d"] < cutoff and r["lab"].get(label) is not None]
        if len(tr) < 100:
            continue
        m = _fit_clf(np.array([r["x"] for r in tr], dtype=float),
                     np.array([r["lab"][label] for r in tr], dtype=float))
        if m is None:
            continue
        ci = {c: i for i, c in enumerate(m["classes"])}
        pb = [r for r in rows if r["d"] in block]
        if not pb:
            continue
        for r, pr in zip(pb, _pred_proba(m, np.array([r["x"] for r in pb], dtype=float))):
            up[(r["t"], r["d"])] = float(pr[ci[1]]) if 1 in ci else 0.0
            down[(r["t"], r["d"])] = float(pr[ci[-1]]) if -1 in ci else 0.0
    return up, down


def backfill(store: FeatureStore | None = None, db_path=DEFAULT_DB) -> dict:
    import numpy as np
    store = store or FeatureStore()
    register_all(store)
    rows, cols, latest = _panel(db_path)
    written = 0
    with Trigger("predictors_multi", stage="S3") as trig:
        tid = trig.trigger_id
        ret_series = {}
        for h in HORIZONS:
            rs = _walk_reg(rows, f"ret_{h}d"); ret_series[h] = rs
            for (t, d), v in rs.items():
                store.write(f"predict.ret_{h}d", t, d, round(v, 6), trigger_id=tid); written += 1
            up, down = _walk_dir(rows, h)
            for (t, d), v in up.items():
                store.write(f"predict.up_{h}d", t, d, round(v, 4), trigger_id=tid); written += 1
            for (t, d), v in down.items():
                store.write(f"predict.down_{h}d", t, d, round(v, 4), trigger_id=tid); written += 1
        for (t, d), v in _walk_reg(rows, f"vol_{VOL_HORIZON}d").items():
            store.write(f"predict.vol_{VOL_HORIZON}d", t, d, round(v, 6), trigger_id=tid); written += 1
        for (t, d), v in ret_series[1].items():           # S4 compat: eod_return == ret_1d
            store.write("predict.eod_return", t, d, round(v, 6), trigger_id=tid)

        # return CI width (σ) per horizon from the final full-history model -> predict.band
        band = {}
        finals = {}
        for h in HORIZONS:
            tr = [r for r in rows if r["lab"].get(f"ret_{h}d") is not None]
            if len(tr) >= 100:
                finals[h] = _fit_reg(np.array([r["x"] for r in tr], dtype=float),
                                     np.array([r["lab"][f"ret_{h}d"] for r in tr], dtype=float))
                band[f"ret_{h}d"] = round(finals[h]["sigma"], 6)
        if band:
            store.write("predict.band", MARKET_SCOPE, latest, band, trigger_id=tid)

        # forecast the future (latest date) with CI + up/down
        dir_finals = {}
        for h in HORIZONS:
            tr = [r for r in rows if r["lab"].get(f"dir_{h}d") is not None]
            if len(tr) >= 100:
                dir_finals[h] = _fit_clf(np.array([r["x"] for r in tr], dtype=float),
                                         np.array([r["lab"][f"dir_{h}d"] for r in tr], dtype=float))
        live = [r for r in rows if r["d"] == latest]
        fc_written = 0
        for r in live:
            close, doc = r["close"], {}
            X = np.array([r["x"]], dtype=float)
            for h in HORIZONS:
                if h not in finals:
                    continue
                ret = float(_pred_reg(finals[h], X)[0]); sig = finals[h]["sigma"]
                pu = pd = None
                if h in dir_finals and dir_finals[h]:
                    pr = _pred_proba(dir_finals[h], X)[0]
                    cidx = {c: i for i, c in enumerate(dir_finals[h]["classes"])}
                    pu = round(float(pr[cidx[1]]), 4) if 1 in cidx else None
                    pd = round(float(pr[cidx[-1]]), 4) if -1 in cidx else None
                doc[f"{h}d"] = {"ahead": f"+{h} trading days", "pred_return": round(ret, 6),
                                "ci_low": round(ret - Z95 * sig, 6), "ci_high": round(ret + Z95 * sig, 6),
                                "pred_price": round(close * (1 + ret), 4),
                                "price_low": round(close * (1 + ret - Z95 * sig), 4),
                                "price_high": round(close * (1 + ret + Z95 * sig), 4),
                                "p_up": pu, "p_down": pd}
            if doc:
                store.write("predict.forecast", r["t"], latest, doc, trigger_id=tid); fc_written += 1
        trig.add_metrics(status="DONE", oos_predictions=written, latest_date=latest,
                         forecasts=fc_written, horizons=HORIZONS)
        return {"trigger_id": tid, **trig.metrics}


if __name__ == "__main__":
    print(json.dumps(backfill(), default=str, indent=2))
