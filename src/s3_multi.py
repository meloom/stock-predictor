"""s3_multi.py — multi-horizon predictors, walk-forward OVER TIME + a forward FORECAST.

Naming: outputs are `<family>_<N>d` (ret_1d, up_5d, down_21d, vol_5d) — horizon = N days.

ONE coherent probabilistic forecast per horizon, from a single return model:
  • return     — Ridge; point ŷ AND a proper leverage-adjusted 95% PREDICTION INTERVAL
                 ŷ ± 1.96·σ·√(1 + xᵀMx), σ² = RSS/(n−tr(H))   -> predict.ret_<N>d,
                 predict.ci_ret_<N>d (per-point half-width)
  • direction  — DERIVED from that same predictive distribution, NOT a separate model:
                 P(up)=Φ(ŷ/se), down=1−P(up), se the interval std. So up and down are
                 coherent (always sum to 1, move with ŷ)       -> predict.up_<N>d, down_<N>d
  • volatility — forward realized vol (Ridge, h=5)             -> predict.vol_5d

Why predict the PAST: the walk-forward series is an OUT-OF-SAMPLE BACKTEST (each block
scored by a model trained only on strictly-earlier dates) — it exists to MEASURE skill
before a model is trusted. The real forecast is predict.forecast at the latest date.
The production model's train_end marks the trust boundary (see serving.py / /predictors).
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
CI_Z = 1.645        # 90% two-sided interval
CI_PCT = 90
RIDGE_LAMBDA = 10.0

# TWO distinct namespaces (do NOT conflate them):
#   backtest.*  — HISTORICAL walk-forward, out-of-sample EVALUATION only. Measures skill
#                 on past dates so we can decide whether to trust the model. NOT a forecast.
#   predict.*   — the PRODUCTION PREDICTION: the promoted/final model applied at the LATEST
#                 date, forecasting the FUTURE (there is no realized outcome yet). One point.
S3M_FEATURES = []
for _h in HORIZONS:
    S3M_FEATURES += [
        (f"backtest.ret_{_h}d", "float", "ticker", "daily",
         f"BACKTEST: walk-forward OOS predicted {_h}d return (evaluation, historical)."),
        (f"backtest.ci_ret_{_h}d", "float", "ticker", "daily",
         f"BACKTEST: 95% PI half-width for the {_h}d return (leverage-adjusted, per point)."),
        (f"backtest.up_{_h}d", "float", "ticker", "daily",
         f"BACKTEST: P(return>0) over {_h}d = Φ(ŷ/se)."),
        (f"backtest.down_{_h}d", "float", "ticker", "daily",
         f"BACKTEST: P(return<0) over {_h}d = 1 − P(up)."),
        (f"predict.ret_{_h}d", "float", "ticker", "daily",
         f"PREDICTION (future): forecast {_h}d-ahead return from the latest date (Ridge)."),
        (f"predict.ci_ret_{_h}d", "float", "ticker", "daily",
         f"PREDICTION: 95% PI half-width for the {_h}d forecast (leverage-adjusted)."),
        (f"predict.up_{_h}d", "float", "ticker", "daily",
         f"PREDICTION: P(return>0) over the next {_h}d = Φ(ŷ/se)."),
        (f"predict.down_{_h}d", "float", "ticker", "daily",
         f"PREDICTION: P(return<0) over the next {_h}d = 1 − P(up)."),
    ]
S3M_FEATURES += [
    (f"backtest.vol_{VOL_HORIZON}d", "float", "ticker", "daily",
     f"BACKTEST: walk-forward OOS predicted realized vol over {VOL_HORIZON}d."),
    (f"predict.vol_{VOL_HORIZON}d", "float", "ticker", "daily",
     f"PREDICTION (future): forecast realized vol over the next {VOL_HORIZON}d."),
    ("predict.forecast", "json", "ticker", "daily",
     "PREDICTION (future): per-horizon forecast at the latest date — price + 95% CI + P(up)/P(down)."),
    ("predict.band", "json", "market", "daily",
     "Per-horizon irreducible residual σ (summary; per-point CIs are leverage-adjusted)."),
]


def register_all(store: FeatureStore) -> None:
    import s3_predictors
    s3_predictors.register_all(store)
    for name, dtype, sk, cadence, rule in S3M_FEATURES:
        store.register(name, dtype, sk, source_stage="S3", cadence=cadence, pit_rule=rule)


# ── panel ──
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
            lab = {f"ret_{h}d": (closes[i + h] / closes[i] - 1
                                 if i + h < len(closes) and closes[i] else None)
                   for h in HORIZONS}
            vw = drets[i:i + VOL_HORIZON]
            lab[f"vol_{VOL_HORIZON}d"] = float(np.std(vw)) if len(vw) == VOL_HORIZON else None
            rows.append({"d": d, "t": t, "x": x, "lab": lab, "close": closes[i]})
    return rows, cols, latest_date


# ── fit / predict + proper prediction interval ──
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
    """Ridge + the pieces of a leverage-adjusted PREDICTION INTERVAL:
      M = (XzᵀXz + λI)⁻¹ ;  σ² = RSS/(n − tr(H)), H = Xz M Xzᵀ.
    Interval std for a query xz is σ·√(1 + xzᵀ M xz) — wider for high-leverage inputs."""
    import numpy as np
    from sklearn.linear_model import Ridge
    mean, std = _norm(X); Xz = _stdz(X, mean, std)
    m = Ridge(alpha=RIDGE_LAMBDA).fit(Xz, y)
    resid = y - m.predict(Xz)
    p = Xz.shape[1]; A = Xz.T @ Xz
    M = np.linalg.inv(A + RIDGE_LAMBDA * np.eye(p))
    edf = float(np.trace(M @ A))
    sigma = float(np.sqrt(np.sum(resid ** 2) / max(1.0, len(y) - edf)))
    return {"m": m, "mean": mean, "std": std, "kind": "reg", "sigma": sigma, "M": M}


def _pred_reg(model, X):
    return model["m"].predict(_stdz(X, model["mean"], model["std"]))


def _pi_se(model, X):
    """Per-row prediction-interval standard error σ·√(1 + leverage)."""
    import numpy as np
    Xz = _stdz(X, model["mean"], model["std"])
    lev = np.clip(np.einsum("ij,jk,ik->i", Xz, model["M"], Xz), 0.0, None)
    return model["sigma"] * np.sqrt(1.0 + lev)


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _blocks(dates):
    start = int(len(dates) * WARMUP_FRAC)
    for b in range(start, len(dates), RETRAIN_EVERY):
        yield dates[b], set(dates[b:b + RETRAIN_EVERY])


def _walk_reg(rows, label, with_se=False):
    """Expanding-window OOS predictions. with_se -> value is (point, interval_std)."""
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
        Xp = np.array([r["x"] for r in pb], dtype=float)
        preds = _pred_reg(m, Xp)
        ses = _pi_se(m, Xp) if with_se else None
        for k, (r, p) in enumerate(zip(pb, preds)):
            out[(r["t"], r["d"])] = (float(p), float(ses[k])) if with_se else float(p)
    return out


# ── model roster: the models we TRY per target (building/evaluation) ──
ROSTER = ["ridge", "gbm", "baseline"]


def _fit_pred(name, Xtr, ytr, Xte):
    import numpy as np
    mean, std = _norm(Xtr)
    Ztr, Zte = _stdz(Xtr, mean, std), _stdz(Xte, mean, std)
    if name == "ridge":
        from sklearn.linear_model import Ridge
        return Ridge(alpha=RIDGE_LAMBDA).fit(Ztr, ytr).predict(Zte)
    if name == "gbm":
        from sklearn.ensemble import HistGradientBoostingRegressor
        return HistGradientBoostingRegressor(max_depth=3, max_iter=150,
                                             learning_rate=0.05).fit(Ztr, ytr).predict(Zte)
    return np.full(len(Xte), float(np.mean(ytr)))            # baseline: predict the mean


def _pearson(xs, ys):
    import numpy as np
    if len(xs) < 3:
        return None
    xs, ys = np.array(xs), np.array(ys)
    dx, dy = xs.std(), ys.std()
    return float(((xs - xs.mean()) * (ys - ys.mean())).mean() / (dx * dy)) if dx and dy else None


def _metrics(pred, real):
    import numpy as np
    pred, real = np.array(pred), np.array(real)
    up = pred > 0; dn = pred < 0
    return {"n": int(len(pred)), "ic": _pearson(pred, real),
            "hit": float(((pred > 0) == (real > 0)).mean()),
            "up_hit": float((real[up] > 0).mean()) if up.any() else None,
            "down_hit": float((real[dn] < 0).mean()) if dn.any() else None}


def score_models(rows, db_path=DEFAULT_DB) -> dict:
    """Building-phase evaluation: for each prediction TARGET, try every model in the
    roster on a held-out split and score its accuracy (IC/hit + up/down hit for returns,
    corr for vol). up/down are METRICS OF A TARGET, not separate predictors."""
    import numpy as np
    dates = sorted({r["d"] for r in rows})
    if len(dates) < 40:
        return {}
    split = dates[int(len(dates) * 0.6)]
    scores = {}
    targets = [(f"ret_{h}d", "return", h) for h in HORIZONS] + [(f"vol_{VOL_HORIZON}d", "volatility", VOL_HORIZON)]
    for lab, fam, h in targets:
        tr = [r for r in rows if r["d"] < split and r["lab"].get(lab) is not None]
        te = [r for r in rows if r["d"] >= split and r["lab"].get(lab) is not None]
        if len(tr) < 100 or not te:
            continue
        Xtr = np.array([r["x"] for r in tr], dtype=float); ytr = np.array([r["lab"][lab] for r in tr], dtype=float)
        Xte = np.array([r["x"] for r in te], dtype=float); yte = np.array([r["lab"][lab] for r in te], dtype=float)
        entry = {"family": fam, "horizon": f"{h}d", "test_from": split, "n_test": len(te), "models": {}}
        for name in ROSTER:
            try:
                pred = _fit_pred(name, Xtr, ytr, Xte)
                entry["models"][name] = ({"n": len(te), "corr": _pearson(pred, yte)}
                                         if fam == "volatility" else _metrics(pred, yte))
            except Exception:
                pass
        # champion = best directional hit (returns) / best corr (vol)
        key = "corr" if fam == "volatility" else "hit"
        best = max((m for m in entry["models"].items() if m[1].get(key) is not None),
                   key=lambda kv: kv[1][key], default=(None, None))
        entry["champion"] = best[0]
        scores[lab] = entry
    return scores


def backfill(store: FeatureStore | None = None, db_path=DEFAULT_DB) -> dict:
    import numpy as np
    store = store or FeatureStore()
    register_all(store)
    rows, cols, latest = _panel(db_path)
    written = 0
    with Trigger("predictors_multi", stage="S3") as trig:
        tid = trig.trigger_id
        # ── 1) BACKTEST: historical walk-forward OOS (EVALUATION only) ──
        for h in HORIZONS:
            for (t, d), (pt, se) in _walk_reg(rows, f"ret_{h}d", with_se=True).items():
                store.write(f"backtest.ret_{h}d", t, d, round(pt, 6), trigger_id=tid)
                store.write(f"backtest.ci_ret_{h}d", t, d, round(CI_Z * se, 6), trigger_id=tid)
                up = _norm_cdf(pt / se) if se > 0 else 0.5
                store.write(f"backtest.up_{h}d", t, d, round(up, 4), trigger_id=tid)
                store.write(f"backtest.down_{h}d", t, d, round(1.0 - up, 4), trigger_id=tid)
                written += 4
        for (t, d), v in _walk_reg(rows, f"vol_{VOL_HORIZON}d").items():
            store.write(f"backtest.vol_{VOL_HORIZON}d", t, d, round(v, 6), trigger_id=tid); written += 1

        # ── 2) CI baseline σ per horizon (summary) ──
        band = {}
        for h in HORIZONS:
            tr = [r for r in rows if r["lab"].get(f"ret_{h}d") is not None]
            if len(tr) >= 100:
                band[f"ret_{h}d"] = round(_fit_reg(
                    np.array([r["x"] for r in tr], dtype=float),
                    np.array([r["lab"][f"ret_{h}d"] for r in tr], dtype=float))["sigma"], 6)
        if band:
            store.write("predict.band", MARKET_SCOPE, latest, band, trigger_id=tid)

        # ── 3) MODEL SCORING: try every roster model per target (building/evaluation) ──
        scores = score_models(rows, db_path)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        (RUNTIME_DIR / "model_scores.json").write_text(json.dumps(
            {"generated_from": latest, "test_split_frac": 0.6, "targets": scores}, indent=2))

        # NOTE: the FUTURE prediction (predict.*) is produced by DEPLOYMENT, not here —
        # see serving.deploy(): it trains on the last N months up to now and forecasts.
        trig.add_metrics(status="DONE", backtest_predictions=written, latest_date=latest,
                         models_scored=sum(len(v["models"]) for v in scores.values()),
                         horizons=HORIZONS)
        return {"trigger_id": tid, **trig.metrics}


if __name__ == "__main__":
    print(json.dumps(backfill(), default=str, indent=2))
