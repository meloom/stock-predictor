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
Z95 = s3_multi.Z95


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
        half = Z95 * se
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
