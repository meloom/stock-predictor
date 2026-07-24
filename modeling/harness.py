"""modeling/harness.py — shared training/eval/promote machinery.

The modeling/ folder is the EXPERIMENTATION zone: try different models with a
proper purged train/val/test setup, measure honestly (return-IC + price-vs-
naive), and only when a model is good enough, PROMOTE it to the models/
registry with metadata + artifact + a loadable wrapper.

This harness is the reusable spine every experiment (modeling/expNN_*.py)
calls, so the split/eval/promote discipline is identical across experiments.

Reuses the pipeline's own machinery so experiments train on EXACTLY what the
live pipeline computes (no train/serve skew): S1 ingest + S2 features +
s3_predictors.assemble_panel / evaluate / evaluate_price.
"""
from __future__ import annotations

import json
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core import FeatureStore                                   # noqa: E402
from s1_data import run_daily_ingestion, fetch_daily_bars, fetch_macro  # noqa: E402
from s2_signals import run_signal_generation                    # noqa: E402
import s3_predictors as pred                                    # noqa: E402

MODELS_DIR = ROOT / "models"
PURGE_DAYS, EMBARGO_DAYS = 15, 7


# ═══════════════ Data prep (real, PIT-correct) ═══════════════

def prepare_panel(universe: list[str], horizon_days: int, period: str = "2y",
                  sample_every: int = 3) -> dict:
    """Ingest real bars + compute S2 features across history + assemble a
    PIT-correct panel. Returns {panel, base_prices, store}."""
    bars = fetch_daily_bars(universe, period=period)
    store = FeatureStore(Path(os.environ.get("TMPDIR", "/tmp")) /
                         f"modeling_{datetime.now(timezone.utc).timestamp()}.db")
    run_daily_ingestion(universe, store=store, fetch_bars=lambda t: bars,
                        fetch_macro=lambda: fetch_macro(period),
                        fetch_dte=lambda t, asof=None: None, fetch_quote=lambda t: None,
                        fetch_shares=lambda t: None,
                        fetch_statements=lambda t, asof=None: None,
                        fetch_analyst=lambda t: None)
    all_dates = sorted({r["date"] for rows in bars.values() for r in rows})
    sample = all_dates[50:-(horizon_days + 2)][::sample_every]
    for d in sample:
        run_signal_generation(universe, d, store=store)
    panel = pred.assemble_panel(store, universe, sample, horizon_days=horizon_days)
    base = [store.read_asof("price.close", t, d)["value"] for d, t in panel["meta"]]
    return {"panel": panel, "base_prices": base, "store": store}


def purged_split(meta: list, train_frac: float = 0.7) -> dict:
    """Time-ordered purged/embargoed split by DATE (not row) — no leakage
    across the boundary."""
    uniq = sorted({d for d, _ in meta})
    cut = int(len(uniq) * train_frac)
    train_end = uniq[cut]
    test_start = uniq[min(cut + EMBARGO_DAYS, len(uniq) - 1)]
    tr = [i for i, (d, _) in enumerate(meta) if d <= train_end]
    te = [i for i, (d, _) in enumerate(meta) if d >= test_start]
    return {"train_idx": tr, "test_idx": te,
            "train_end": train_end, "test_start": test_start}


# ═══════════════ Generic fit (any sklearn estimator) ═══════════════

def fit(X, y, estimator) -> dict:
    """Standardize + mean-impute + fit any sklearn estimator. Returns the same
    self-contained trained dict shape s3_predictors uses, so evaluate() /
    predict work regardless of model type."""
    import numpy as np
    mean = np.nanmean(X, axis=0)
    std = np.nanstd(X, axis=0)
    std[std == 0] = 1.0
    Xz = np.where(np.isnan(X), 0.0, (X - mean) / std)
    model = estimator.fit(Xz, y)
    coefs = None
    if hasattr(model, "coef_"):
        coefs = dict(zip(pred.PREDICTOR_FEATURES, np.ravel(model.coef_).tolist()))
    return {"model": model, "mean": mean, "std": std,
            "feature_names": list(pred.PREDICTOR_FEATURES), "coefficients": coefs}


def evaluate_all(trained, panel, base_prices, split) -> dict:
    """Both views: cross-sectional return (IC/MSE-vs-null) + price-vs-naive."""
    import numpy as np
    te = split["test_idx"]
    X, y = panel["X"][te], panel["y"][te]
    ret = pred.evaluate(trained, X, y)
    pred_ret = pred._predict_vec(trained, X).tolist()
    price = pred.evaluate_price(pred_ret, y.tolist(), [base_prices[i] for i in te])
    return {"return": ret, "price": price}


# ═══════════════ Promotion to the models/ registry ═══════════════

def promote(model_id: str, trained: dict, metadata: dict) -> Path:
    """Write the artifact + metadata into models/<model_id>/ and register it.
    Artifact (.pkl) is gitignored (rebuild via the experiment); metadata is
    committed. Registration is what makes a model loadable by the wrapper."""
    mdir = MODELS_DIR / model_id
    mdir.mkdir(parents=True, exist_ok=True)
    with open(mdir / "artifact.pkl", "wb") as f:
        pickle.dump(trained, f)
    meta = {**metadata, "model_id": model_id,
            "feature_names": trained["feature_names"],
            "promoted_at": datetime.now(timezone.utc).isoformat()}
    with open(mdir / "metadata.json", "w") as f:
        json.dump(meta, f, indent=2)
    # update registry index
    reg_path = MODELS_DIR / "registry.json"
    reg = json.loads(reg_path.read_text()) if reg_path.exists() else {"models": {}}
    reg["models"][model_id] = {"promoted_at": meta["promoted_at"],
                               "metrics": metadata.get("test_metrics")}
    reg_path.write_text(json.dumps(reg, indent=2))
    return mdir


def meets_bar(metrics: dict, min_ic: float = 0.03,
              require_beats_naive: bool = True) -> bool:
    """The promotion gate. A model is 'good enough' only if it clears real
    thresholds — a model that doesn't beat the null/naive is NOT promoted,
    and that is a valid, honest outcome."""
    ret, price = metrics["return"], metrics["price"]
    if ret["ic"] is None or ret["ic"] < min_ic:
        return False
    if not ret["beats_null"]:
        return False
    if require_beats_naive and not price["model_beats_naive_rmse"]:
        return False
    return True
