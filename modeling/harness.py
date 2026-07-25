"""modeling/harness.py — shared training/eval/promote machinery.

Protocol (fixed, non-negotiable per owner direction):
  - TRAINING window = 4 weeks (20 trading days).
  - DEV/EVAL window = the 2 weeks (10 trading days) AFTER training.
  - A purge gap = the label horizon sits between them (so training labels,
    which look `horizon` days ahead, cannot overlap the dev window).
  - ALWAYS the full tracked universe (src/universe.py).
  - One model per file (modeling/model_*.py); every run appends its metrics +
    metadata to the shared modeling/performance.log.

Metadata logged per run: model, label strategy, training-data range, dev-data
range, tickers, features, and the held-out metrics.

Reuses the pipeline's own S1/S2 code so training sees EXACTLY what the live
pipeline computes (no train/serve skew).
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core import FeatureStore                                   # noqa: E402
from universe import UNIVERSE                                   # noqa: E402
from s1_data import (run_daily_ingestion, fetch_daily_bars, fetch_macro,   # noqa: E402
                     fetch_shares_outstanding, fetch_latest_statements,
                     fetch_analyst_snapshot)
from s2_signals import run_signal_generation                    # noqa: E402
import s3_predictors as pred                                    # noqa: E402

MODELS_DIR = ROOT / "models"
MODELING_DIR = ROOT / "modeling"
PERF_LOG = MODELING_DIR / "performance.log"

TRAIN_DAYS = 20      # 4 weeks of trading days
DEV_DAYS = 10        # 2 weeks of trading days
LABEL_STRATEGY = "end_of_day_forward_return"   # predict close H trading days ahead
MOVE_THRESHOLD = 0.03   # big-move label: |fwd return| > this => up/down, else neutral


# ═══════════════ STANDARDIZED data collection (one path for train + eval) ════
# Every model script uses build_full_dataset + make_labels + rolling_windows so
# the split/labels/standardization are identical everywhere — no ad-hoc per-
# script data prep.

def build_full_dataset(horizon_days: int = 1, universe: list[str] | None = None,
                       period: str = "1y", with_fundamentals: bool = True,
                       cache: bool = True) -> dict:
    """ONE fetch -> S2 features across the WHOLE period -> a panel spanning all
    trading days (so any 4wk/2wk window, or many rolling windows, can be sliced
    without re-fetching). Cached to runtime/full_dataset_h{H}.pkl."""
    import pickle
    universe = universe or UNIVERSE
    cache_p = (ROOT / "runtime" / f"full_dataset_h{horizon_days}.pkl")
    bars = fetch_daily_bars(universe, period=period)
    store = FeatureStore(Path(os.environ.get("TMPDIR", "/tmp")) /
                         f"fullds_{datetime.now(timezone.utc).timestamp()}.db")
    if with_fundamentals:
        sh = {t: fetch_shares_outstanding(t) for t in universe}
        st = {t: fetch_latest_statements(t) for t in universe}
        an = {t: fetch_analyst_snapshot(t) for t in universe}
        fs, fst, fan = (lambda t: sh.get(t)), (lambda t, asof=None: st.get(t)), (lambda t: an.get(t))
    else:
        fs = fst = fan = (lambda t, asof=None: None)
    run_daily_ingestion(universe, store=store, fetch_bars=lambda t: bars,
                        fetch_macro=lambda: fetch_macro(period),
                        fetch_dte=lambda t, asof=None: None, fetch_quote=lambda t: None,
                        fetch_shares=fs, fetch_statements=fst, fetch_analyst=fan)
    all_dates = sorted({r["date"] for rows in bars.values() for r in rows})
    usable = all_dates[35:-(horizon_days + 1)]   # warmup for S2 history + fwd room
    for d in usable:
        run_signal_generation(universe, d, store=store)
    panel = pred.assemble_panel(store, universe, usable, horizon_days=horizon_days)
    base = [store.read_asof("price.close", t, d)["value"] for d, t in panel["meta"]]
    out = {"panel": panel, "base_prices": base, "feature_names": panel["feature_names"],
           "universe": universe, "horizon_days": horizon_days,
           "label_strategy": f"{LABEL_STRATEGY}(H={horizon_days}d)"}
    if cache:
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_p, "wb") as f:
            pickle.dump(out, f)
    return out


def load_full_dataset(horizon_days: int = 1):
    import pickle
    p = ROOT / "runtime" / f"full_dataset_h{horizon_days}.pkl"
    return pickle.load(open(p, "rb")) if p.exists() else None


def make_labels(y, move: float = MOVE_THRESHOLD):
    """Big-move 3-class labels: +1 up (>+move), -1 down (<-move), 0 neutral."""
    import numpy as np
    y = np.asarray(y, float)
    return np.where(y > move, 1, np.where(y < -move, -1, 0))


def rolling_windows(meta, horizon_days: int = 1, step_days: int = 10):
    """Yield successive standardized 4wk-train / 2wk-dev windows across history
    (purge = horizon between them). Each: {train_idx, dev_idx, train_range,
    dev_range}. This is how up/down precision is measured across MANY regimes,
    not one lucky/unlucky fortnight."""
    dates = sorted({d for d, _ in meta})
    span = TRAIN_DAYS + horizon_days + DEV_DAYS
    out = []
    start = 0
    while start + span <= len(dates):
        tr_dates = dates[start:start + TRAIN_DAYS]
        dv_dates = dates[start + TRAIN_DAYS + horizon_days:
                         start + TRAIN_DAYS + horizon_days + DEV_DAYS]
        tr_set, dv_set = set(tr_dates), set(dv_dates)
        tr = [i for i, (d, _) in enumerate(meta) if d in tr_set]
        dv = [i for i, (d, _) in enumerate(meta) if d in dv_set]
        out.append({"train_idx": tr, "dev_idx": dv,
                    "train_range": [tr_dates[0], tr_dates[-1]],
                    "dev_range": [dv_dates[0], dv_dates[-1]]})
        start += step_days
    return out


# ═══════════════ Data prep: fixed 4wk-train / 2wk-dev window, full universe ═══════════════

def prepare_window(horizon_days: int = 1, universe: list[str] | None = None,
                   period: str = "1y", with_fundamentals: bool = True) -> dict:
    """Build a PIT-correct panel over the most recent 4-week-train + 2-week-dev
    window (with a `horizon_days` purge between them). Uses the full universe.
    with_fundamentals=True populates the fund.* features (slower — fetches
    statements/shares/analyst per ticker, once). Returns {panel, base_prices,
    split, ranges}."""
    universe = universe or UNIVERSE
    bars = fetch_daily_bars(universe, period=period)
    store = FeatureStore(Path(os.environ.get("TMPDIR", "/tmp")) /
                         f"modeling_{datetime.now(timezone.utc).timestamp()}.db")
    if with_fundamentals:
        sh = {t: fetch_shares_outstanding(t) for t in universe}
        st = {t: fetch_latest_statements(t) for t in universe}
        an = {t: fetch_analyst_snapshot(t) for t in universe}
        fetch_shares = lambda t: sh.get(t)
        fetch_statements = lambda t, asof=None: st.get(t)
        fetch_analyst = lambda t: an.get(t)
    else:
        fetch_shares = lambda t: None
        fetch_statements = lambda t, asof=None: None
        fetch_analyst = lambda t: None
    run_daily_ingestion(universe, store=store, fetch_bars=lambda t: bars,
                        fetch_macro=lambda: fetch_macro(period),
                        fetch_dte=lambda t, asof=None: None, fetch_quote=lambda t: None,
                        fetch_shares=fetch_shares, fetch_statements=fetch_statements,
                        fetch_analyst=fetch_analyst)

    dates = sorted({r["date"] for rows in bars.values() for r in rows})
    n_ok = sum(1 for rows in bars.values() if rows)
    usable = dates[:-horizon_days] if horizon_days > 0 else dates   # need forward price
    need = TRAIN_DAYS + horizon_days + DEV_DAYS
    if len(usable) < need:
        raise ValueError(
            f"not enough trading days for a 4wk/2wk window: got {len(dates)} "
            f"dates ({n_ok}/{len(universe)} tickers returned bars) — need >= "
            f"{need}. Likely a yfinance rate-limit/sparse fetch; retry later.")
    dev_dates = usable[-DEV_DAYS:]
    e0 = dates.index(dev_dates[0])
    train_dates = dates[e0 - horizon_days - TRAIN_DAYS: e0 - horizon_days]
    assert len(train_dates) == TRAIN_DAYS and len(dev_dates) == DEV_DAYS, \
        "window guarantee violated (must be 20 train / 10 dev trading days)"

    window = train_dates + dev_dates
    for d in window:
        run_signal_generation(universe, d, store=store)
    panel = pred.assemble_panel(store, universe, window, horizon_days=horizon_days)

    tr = set(train_dates)
    dv = set(dev_dates)
    train_idx = [i for i, (d, _) in enumerate(panel["meta"]) if d in tr]
    dev_idx = [i for i, (d, _) in enumerate(panel["meta"]) if d in dv]
    base = [store.read_asof("price.close", t, d)["value"] for d, t in panel["meta"]]
    return {"panel": panel, "base_prices": base,
            "split": {"train_idx": train_idx, "dev_idx": dev_idx},
            "ranges": {"train_range": [train_dates[0], train_dates[-1]],
                       "dev_range": [dev_dates[0], dev_dates[-1]],
                       "purge_days": horizon_days,
                       "tickers": universe,
                       "features": list(pred.PREDICTOR_FEATURES),
                       "label_strategy": f"{LABEL_STRATEGY}(H={horizon_days}d)",
                       "horizon_days": horizon_days}}


# ═══════════════ Fit / evaluate ═══════════════

def fit(X, y, estimator) -> dict:
    """Standardize + mean-impute + fit any sklearn estimator."""
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


def evaluate_at(trained, panel, base_prices, idx) -> dict:
    """Return view (IC/MSE-vs-null) + price view (vs naive) on the dev rows."""
    X, y = panel["X"][idx], panel["y"][idx]
    ret = pred.evaluate(trained, X, y)
    pr = pred._predict_vec(trained, X).tolist()
    price = pred.evaluate_price(pr, y.tolist(), [base_prices[i] for i in idx])
    return {"return": ret, "price": price}


# ═══════════════ Performance log (shared, append-only) ═══════════════

def log_performance(model_name: str, ranges: dict, metrics: dict,
                    promoted: bool, extra: dict | None = None) -> dict:
    """Append one record to modeling/performance.log — the common log of every
    training run: model, label strategy, train/dev ranges, tickers, features,
    metrics, promotion outcome."""
    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "label_strategy": ranges["label_strategy"],
        "train_range": ranges["train_range"],
        "dev_range": ranges["dev_range"],
        "n_tickers": len(ranges["tickers"]),
        "tickers": ranges["tickers"],
        "features": ranges["features"],
        # plain metrics only (no "IC"): price error + direction hit-rate
        "dev_price_rmse": metrics["price"]["model"]["rmse"],
        "dev_naive_price_rmse": metrics["price"]["naive_persistence"]["rmse"],
        "dev_price_mape_pct": metrics["price"]["model"]["mape_pct"],
        "dev_naive_mape_pct": metrics["price"]["naive_persistence"]["mape_pct"],
        "dev_direction_hit_rate": metrics["return"]["direction_hit_rate"],
        "dev_return_rmse": metrics["return"]["rmse"],
        "dev_beats_null": metrics["return"]["beats_null"],
        "dev_beats_naive": metrics["price"]["model_beats_naive_rmse"],
        "promoted": promoted,
        **(extra or {}),
    }
    PERF_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(PERF_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record


# ═══════════════ Promotion ═══════════════

def meets_bar(metrics: dict, min_ic: float = 0.03,
              require_beats_naive: bool = True) -> bool:
    ret, price = metrics["return"], metrics["price"]
    if ret["ic"] is None or ret["ic"] < min_ic or not ret["beats_null"]:
        return False
    if require_beats_naive and not price["model_beats_naive_rmse"]:
        return False
    return True


def promote(model_id: str, trained: dict, ranges: dict, metrics: dict) -> Path:
    """Write artifact + metadata into models/<model_id>/ and register it.
    Metadata carries: train/dev ranges, tickers, features, label strategy,
    test metrics."""
    mdir = MODELS_DIR / model_id
    mdir.mkdir(parents=True, exist_ok=True)
    with open(mdir / "artifact.pkl", "wb") as f:
        pickle.dump(trained, f)
    meta = {"model_id": model_id, "label_strategy": ranges["label_strategy"],
            "train_range": ranges["train_range"], "dev_range": ranges["dev_range"],
            "tickers": ranges["tickers"], "features": ranges["features"],
            "horizon_days": ranges["horizon_days"], "test_metrics": metrics,
            "promoted_at": datetime.now(timezone.utc).isoformat()}
    (mdir / "metadata.json").write_text(json.dumps(meta, indent=2))
    reg_path = MODELS_DIR / "registry.json"
    reg = json.loads(reg_path.read_text()) if reg_path.exists() else {"models": {}}
    reg["models"][model_id] = {"promoted_at": meta["promoted_at"],
                               "train_range": meta["train_range"],
                               "dev_range": meta["dev_range"],
                               "dev_ic": metrics["return"]["ic"]}
    reg_path.write_text(json.dumps(reg, indent=2))
    return mdir
