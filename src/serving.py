"""serving.py — the PREDICTION TRIGGER + production-model registry.

ONE entry point, predict(ticker, asof), that every caller uses the same way — the
dashboard, the Alpha report (S4), and any downstream step. The flow on each call:

  1. load the PROMOTED production model (frozen with its training window);
  2. REJECT if `asof` falls within that training window — we never predict on data the
     model was trained on (that would be leakage, not a forecast);
  3. TRIGGER S2 to compose the feature vector as-of `asof` (run it if not already there);
  4. run the production model and return a per-horizon forecast.

Promotion is deliberate: you optimize a model offline until satisfied, then promote()
freezes it (estimator + standardization + training window) as THE production model.
Serving only ever uses the promoted model — never an ad-hoc retrain.
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


# ── promotion: freeze an optimized model (with its training window) as production ──
def promote(train_end: str, db_path=DEFAULT_DB) -> dict:
    """Train each predictor family on all labeled data with date <= train_end, and save
    the bundle as THE production model. `train_end` is the frozen boundary: serving will
    reject any asof <= train_end."""
    import numpy as np
    rows, cols, latest = s3_multi._panel(db_path)
    pool = [r for r in rows if r["d"] <= train_end]
    dates = sorted({r["d"] for r in pool})
    targets = ([(f"ret_h{h}", "ridge") for h in s3_multi.HORIZONS]
               + [(f"up_h{h}", "logistic") for h in s3_multi.HORIZONS]
               + [(f"vol_h{s3_multi.VOL_HORIZON}", "ridge")])
    models = {}
    for lab, kind in targets:
        tr = [r for r in pool if r["lab"].get(lab) is not None]
        if len(tr) < 100:
            continue
        m = s3_multi._fit(np.array([r["x"] for r in tr], dtype=float),
                          np.array([r["lab"][lab] for r in tr], dtype=float), kind)
        if m:
            models[lab] = m
    bundle = {"train_start": dates[0] if dates else None, "train_end": train_end,
              "features": list(PREDICTOR_FEATURES), "models": models,
              "horizons": s3_multi.HORIZONS, "vol_horizon": s3_multi.VOL_HORIZON,
              "n_train_rows": len(pool)}
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
    """Is S2 already composed for `asof`? (any ticker with a tech feature on that date)"""
    con = sqlite3.connect(Path(db_path))
    n = con.execute("SELECT 1 FROM feature_values WHERE feature='tech.rsi14' "
                    "AND event_time=? LIMIT 1", (asof,)).fetchone()
    return n is not None


# ── the trigger: predict(ticker, asof) ──
def predict(ticker: str, asof: str, store: FeatureStore | None = None,
            db_path=DEFAULT_DB, trigger_s2: bool = True) -> dict:
    """Predict `ticker` as-of `asof` with the production model. Rejects asof inside the
    training window; triggers S2 to compose features if needed; returns per-horizon
    forecast. This is the shared entry point for the dashboard, Alpha report, downstream."""
    import numpy as np
    ticker = (ticker or "").upper()
    b = load_production()
    if b is None:
        return {"status": "NO_PRODUCTION_MODEL",
                "reason": "no model promoted — call serving.promote(train_end) first"}
    # (2) leakage guard: never predict on training data
    if asof <= b["train_end"]:
        return {"status": "REJECTED", "ticker": ticker, "asof": asof,
                "reason": f"asof {asof} is within the model's training window "
                          f"(train_end = {b['train_end']}); predicting on training data "
                          f"is leakage, not a forecast.",
                "train_start": b["train_start"], "train_end": b["train_end"]}
    store = store or FeatureStore()
    # (3) trigger S2 to compose the as-of feature vector if it isn't there yet
    composed = False
    if trigger_s2 and not _s2_ready(asof, db_path):
        s2_signals.run_signal_generation(list(UNIVERSE), asof, store=store)
        composed = True
    px = store.read_asof("price.close", ticker, asof)
    x = np.array([[(store.read_asof(f, ticker, asof) or {}).get("value", np.nan)
                   for f in b["features"]]], dtype=float)
    if px is None or np.all(np.isnan(x)):
        return {"status": "NO_DATA", "ticker": ticker, "asof": asof,
                "reason": f"no price / S2 feature vector for {ticker} as of {asof}",
                "s2_composed_now": composed}
    # (4) run the production model
    price = px["value"]
    preds = {}
    for h in b["horizons"]:
        rm = b["models"].get(f"ret_h{h}")
        if not rm:
            continue
        ret = float(s3_multi._pred(rm, x)[0])
        um = b["models"].get(f"up_h{h}")
        up = float(s3_multi._pred(um, x)[0]) if um else None
        preds[str(h)] = {"ahead": f"+{h} trading days", "pred_return": round(ret, 6),
                         "pred_price": round(price * (1 + ret), 4),
                         "p_up": round(up, 4) if up is not None else None}
    vm = b["models"].get(f"vol_h{b['vol_horizon']}")
    vol = float(s3_multi._pred(vm, x)[0]) if vm else None
    return {"status": "OK", "ticker": ticker, "asof": asof, "price": round(price, 4),
            "predictions": preds, f"pred_vol_h{b['vol_horizon']}": round(vol, 6) if vol is not None else None,
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
