"""serving.py — the PREDICTION TRIGGER + production-model registry.

ONE entry point, predict(ticker, asof), used the same way by every caller — the
dashboard, the Alpha report (S4), any downstream step. On each call:

  1. load the PROMOTED production model (frozen estimators + training window);
  2. REJECT if `asof` is within the training window (never predict on training data);
  3. TRIGGER S2 to compose the feature vector as-of `asof` (run it if absent);
  4. run the model -> per-horizon forecast: return with a 95% CONFIDENCE INTERVAL,
     P(up) / P(down), and predicted price band.

promote(train_end) freezes an optimized model as production; serving never ad-hoc-retrains.
"""
from __future__ import annotations

import json
import pickle
import sqlite3
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import FeatureStore, DEFAULT_DB, RUNTIME_DIR            # noqa: E402
from s3_predictors import PREDICTOR_FEATURES                      # noqa: E402
import s3_multi                                                   # noqa: E402
import s2_signals                                                 # noqa: E402
from universe import UNIVERSE                                     # noqa: E402

PROD_PKL = RUNTIME_DIR / "production_model.pkl"
PROD_META = RUNTIME_DIR / "production_model.json"
CI_Z = s3_multi.CI_Z


def promote(train_end: str, db_path=DEFAULT_DB) -> dict:
    """Train each predictor on all labeled data with date <= train_end; freeze as
    production. Serving rejects any asof <= train_end."""
    import numpy as np
    rows, cols, latest = s3_multi._panel(db_path)
    pool = [r for r in rows if r["d"] <= train_end]
    dates = sorted({r["d"] for r in pool})
    models = {}
    for h in s3_multi.HORIZONS:
        tr = [r for r in pool if r["lab"].get(f"ret_{h}d") is not None]
        if len(tr) >= 100:                                   # one return model per horizon;
            models[f"ret_{h}d"] = s3_multi._fit_reg(         # direction is derived from it
                np.array([r["x"] for r in tr], dtype=float),
                np.array([r["lab"][f"ret_{h}d"] for r in tr], dtype=float))
    vh = s3_multi.VOL_HORIZON
    tv = [r for r in pool if r["lab"].get(f"vol_{vh}d") is not None]
    if len(tv) >= 100:
        models[f"vol_{vh}d"] = s3_multi._fit_reg(
            np.array([r["x"] for r in tv], dtype=float),
            np.array([r["lab"][f"vol_{vh}d"] for r in tv], dtype=float))
    bundle = {"train_start": dates[0] if dates else None, "train_end": train_end,
              "features": list(PREDICTOR_FEATURES), "models": models,
              "horizons": s3_multi.HORIZONS, "vol_horizon": vh, "n_train_rows": len(pool)}
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROD_PKL, "wb") as f:
        pickle.dump(bundle, f)
    meta = {"train_start": bundle["train_start"], "train_end": train_end,
            "horizons": bundle["horizons"], "n_train_rows": bundle["n_train_rows"],
            "predictors": sorted(models)}
    with open(PROD_META, "w") as f:
        json.dump(meta, f, indent=2)
    return meta


def deploy(months: int = 3, db_path=DEFAULT_DB, store=None) -> dict:
    """DEPLOYMENT (≠ model building): train on the LAST `months` of data up to now, then
    forecast the FUTURE. Model building uses a held-out backtest to pick/validate a model;
    deployment retrains that model on the freshest window and predicts forward. Writes the
    production bundle AND predict.* / predict.forecast (the future prediction)."""
    import numpy as np
    from datetime import date, timedelta
    rows, cols, latest = s3_multi._panel(db_path)
    start = (date.fromisoformat(latest) - timedelta(days=int(round(months * 30.44)))).isoformat()
    pool = [r for r in rows if start <= r["d"] <= latest]
    models = {}
    for h in s3_multi.HORIZONS:
        tr = [r for r in pool if r["lab"].get(f"ret_{h}d") is not None]
        if len(tr) >= 60:
            models[f"ret_{h}d"] = s3_multi._fit_reg(
                np.array([r["x"] for r in tr], dtype=float),
                np.array([r["lab"][f"ret_{h}d"] for r in tr], dtype=float))
    vh = s3_multi.VOL_HORIZON
    tv = [r for r in pool if r["lab"].get(f"vol_{vh}d") is not None]
    if len(tv) >= 60:
        models[f"vol_{vh}d"] = s3_multi._fit_reg(
            np.array([r["x"] for r in tv], dtype=float),
            np.array([r["lab"][f"vol_{vh}d"] for r in tv], dtype=float))
    bundle = {"train_start": start, "train_end": latest, "features": list(PREDICTOR_FEATURES),
              "models": models, "horizons": s3_multi.HORIZONS, "vol_horizon": vh,
              "n_train_rows": len(pool), "mode": "deploy", "train_months": months}
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROD_PKL, "wb") as f:
        pickle.dump(bundle, f)
    meta = {"mode": "deploy", "train_months": months, "train_start": start, "train_end": latest,
            "n_train_rows": len(pool), "predictors": sorted(models),
            "forecast_from": latest, "ci_pct": s3_multi.CI_PCT}
    PROD_META.write_text(json.dumps(meta, indent=2))

    # write the FUTURE prediction (predict.* at the latest date) + forecast json
    store = store or FeatureStore()
    s3_multi.register_all(store)
    Z = s3_multi.CI_Z
    for r in [r for r in rows if r["d"] == latest]:
        t, close, doc = r["t"], r["close"], {}
        X = np.array([r["x"]], dtype=float)
        for h in s3_multi.HORIZONS:
            rm = models.get(f"ret_{h}d")
            if not rm:
                continue
            ret = float(s3_multi._pred_reg(rm, X)[0]); se = float(s3_multi._pi_se(rm, X)[0])
            up = s3_multi._norm_cdf(ret / se) if se > 0 else 0.5; half = Z * se
            store.write(f"predict.ret_{h}d", t, latest, round(ret, 6), trigger_id="deploy")
            store.write(f"predict.ci_ret_{h}d", t, latest, round(half, 6), trigger_id="deploy")
            store.write(f"predict.up_{h}d", t, latest, round(up, 4), trigger_id="deploy")
            store.write(f"predict.down_{h}d", t, latest, round(1 - up, 4), trigger_id="deploy")
            doc[f"{h}d"] = {"ahead": f"+{h} trading days", "pred_return": round(ret, 6),
                            "ci_low": round(ret - half, 6), "ci_high": round(ret + half, 6),
                            "pred_price": round(close * (1 + ret), 4),
                            "price_low": round(close * (1 + ret - half), 4),
                            "price_high": round(close * (1 + ret + half), 4),
                            "p_up": round(up, 4), "p_down": round(1 - up, 4)}
        vm = models.get(f"vol_{vh}d")
        if vm is not None:
            store.write(f"predict.vol_{vh}d", t, latest, round(float(s3_multi._pred_reg(vm, X)[0]), 6),
                        trigger_id="deploy")
        if doc:
            store.write("predict.forecast", t, latest, doc, trigger_id="deploy")
    return meta


def deploy_classifier(db_path=DEFAULT_DB, horizons=(1, 3, 5, 7), thr=0.03, store=None,
                      as_of=None) -> dict:
    """Deploy the REAL working model: the big-move DUAL classifier (logistic-up +
    HistGBM-down), calibrated (isotonic), per ticker, for each horizon category. Unlike
    the Ridge Φ(ŷ/se) — which squishes P(up) to ~0.5 for every stock — this predicts
    P(|move|>thr) which genuinely DISCRIMINATES and ranks (the recorded precision@k edge).
    Writes predict.pbig_up_<N>d / predict.pbig_down_<N>d per ticker."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.calibration import CalibratedClassifierCV
    feats, price, cols = s3_multi._load(db_path)
    latest = max((d for pm in price.values() for d in pm), default=None)
    as_of = as_of or latest                                  # the anchor: predict FROM here
    store = store or FeatureStore()
    for h in horizons:
        for side in ("up", "down"):
            store.register(f"predict.pbig_{side}_{h}d", "float", "ticker", "S3", "daily",
                           f"Calibrated P({side}-move > {thr:.0%} over {h}d) — big-move "
                           f"classifier (dual: logistic-up + histgbm-down).")
    store.register("predict.dir_1d", "float", "ticker", "S3", "daily",
                   "Directional score = P(up-big) − P(down-big) at 1d — S4's input.")
    # LEAKAGE-FREE labels: only rows STRICTLY BEFORE as_of whose forward outcome is also
    # realized BY as_of (outcome date <= as_of). The anchor row itself is never trained on.
    lab = {h: [] for h in horizons}                          # h -> [(x, fwd_ret)]
    max_train = None
    for t, pm in price.items():
        ds = sorted(pm); cl = [pm[d] for d in ds]
        for i, d in enumerate(ds):
            if d >= as_of:                                   # never train on the anchor or later
                continue
            x = feats.get((t, d))
            if x is None:
                continue
            for h in horizons:
                j = i + h
                if j < len(ds) and ds[j] <= as_of and cl[i]:  # outcome known BY as_of
                    lab[h].append((x, cl[j] / cl[i] - 1))
                    max_train = d if (max_train is None or d > max_train) else max_train
    live = [(t, feats[(t, as_of)]) for t in price if (t, as_of) in feats]
    Xl = np.array([x for _, x in live], dtype=float)
    meta = {"model": "dual: logistic-up + histgbm-down (isotonic-calibrated)",
            "thr": thr, "latest": latest, "horizons": list(horizons),
            "n_tickers": len(live), "spread": {}}
    for h in horizons:
        data = lab[h]
        X = np.array([x for x, _ in data], dtype=float)
        mean, std = s3_multi._norm(X); Xz = s3_multi._stdz(X, mean, std)
        yu = np.array([1 if r > thr else 0 for _, r in data])
        yd = np.array([1 if r < -thr else 0 for _, r in data])
        Xlz = s3_multi._stdz(Xl, mean, std)

        def cal(est, y):
            if y.sum() < 25:
                return np.zeros(len(live))
            m = CalibratedClassifierCV(est, method="isotonic", cv=3).fit(Xz, y)
            return m.predict_proba(Xlz)[:, 1]
        pu = cal(LogisticRegression(max_iter=500, C=0.5), yu)
        pd = cal(HistGradientBoostingClassifier(max_depth=3, max_iter=150, learning_rate=0.05), yd)
        for i, (t, _) in enumerate(live):
            store.write(f"predict.pbig_up_{h}d", t, latest, round(float(pu[i]), 4), trigger_id="deploy_clf")
            store.write(f"predict.pbig_down_{h}d", t, latest, round(float(pd[i]), 4), trigger_id="deploy_clf")
            if h == 1:                                        # directional score for S4 alpha
                store.write("predict.dir_1d", t, latest, round(float(pu[i] - pd[i]), 4), trigger_id="deploy_clf")
        meta["spread"][f"{h}d"] = {"up": [round(float(pu.min()), 3), round(float(pu.max()), 3)],
                                   "down": [round(float(pd.min()), 3), round(float(pd.max()), 3)]}
    (RUNTIME_DIR / "deployed_classifier.json").write_text(json.dumps(meta, indent=2))
    return meta


def load_production():
    if not PROD_PKL.exists():
        return None
    with open(PROD_PKL, "rb") as f:
        return pickle.load(f)


def _s2_ready(asof: str, db_path=DEFAULT_DB) -> bool:
    con = sqlite3.connect(Path(db_path))
    return con.execute("SELECT 1 FROM feature_values WHERE feature='tech.rsi14' "
                       "AND event_time=? LIMIT 1", (asof,)).fetchone() is not None


def predict(ticker: str, asof: str, store: FeatureStore | None = None,
            db_path=DEFAULT_DB, trigger_s2: bool = True) -> dict:
    """Predict `ticker` as-of `asof` with the production model. Shared trigger for the
    dashboard / Alpha report / downstream."""
    import numpy as np
    ticker = (ticker or "").upper()
    b = load_production()
    if b is None:
        return {"status": "NO_PRODUCTION_MODEL",
                "reason": "no model promoted — call serving.promote(train_end) first"}
    if asof <= b["train_end"]:
        return {"status": "REJECTED", "ticker": ticker, "asof": asof,
                "reason": f"asof {asof} is within the model's training window "
                          f"(train_end = {b['train_end']}); predicting on training data "
                          f"is leakage, not a forecast.",
                "train_start": b["train_start"], "train_end": b["train_end"]}
    store = store or FeatureStore()
    composed = False
    if trigger_s2 and not _s2_ready(asof, db_path):
        s2_signals.run_signal_generation(list(UNIVERSE), asof, store=store)
        composed = True
    px = store.read_asof("price.close", ticker, asof)
    X = np.array([[(store.read_asof(f, ticker, asof) or {}).get("value", np.nan)
                  for f in b["features"]]], dtype=float)
    if px is None or np.all(np.isnan(X)):
        return {"status": "NO_DATA", "ticker": ticker, "asof": asof,
                "reason": f"no price / S2 feature vector for {ticker} as of {asof}",
                "s2_composed_now": composed}
    price = px["value"]; preds = {}
    for h in b["horizons"]:
        rm = b["models"].get(f"ret_{h}d")
        if not rm:
            continue
        ret = float(s3_multi._pred_reg(rm, X)[0])
        se = float(s3_multi._pi_se(rm, X)[0])            # leverage-adjusted interval std
        half = CI_Z * se
        up = s3_multi._norm_cdf(ret / se) if se > 0 else 0.5   # direction from the forecast
        preds[f"{h}d"] = {"ahead": f"+{h} trading days", "pred_return": round(ret, 6),
                          "ci_low": round(ret - half, 6), "ci_high": round(ret + half, 6),
                          "pred_price": round(price * (1 + ret), 4),
                          "price_low": round(price * (1 + ret - half), 4),
                          "price_high": round(price * (1 + ret + half), 4),
                          "p_up": round(up, 4), "p_down": round(1 - up, 4)}
    vh = b["vol_horizon"]; vm = b["models"].get(f"vol_{vh}d")
    vol = float(s3_multi._pred_reg(vm, X)[0]) if vm else None
    return {"status": "OK", "ticker": ticker, "asof": asof, "price": round(price, 4),
            "predictions": preds, f"pred_vol_{vh}d": round(vol, 6) if vol is not None else None,
            "model": {"train_start": b["train_start"], "train_end": b["train_end"]},
            "s2_composed_now": composed}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("promote"); p.add_argument("train_end")
    q = sub.add_parser("predict"); q.add_argument("ticker"); q.add_argument("asof")
    a = ap.parse_args()
    print(json.dumps(promote(a.train_end) if a.cmd == "promote"
                     else predict(a.ticker, a.asof), default=str, indent=2))
