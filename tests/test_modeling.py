"""Modeling tests: fit any estimator, the promotion gate, and the
promote -> registry -> wrapper.load -> predict roundtrip. Offline/synthetic."""
import sys
from pathlib import Path
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modeling"))
sys.path.insert(0, str(ROOT / "models"))
sys.path.insert(0, str(ROOT / "src"))

import harness as H
import wrapper as W
from s3_predictors import PREDICTOR_FEATURES


def _planted_panel(n=2000):
    rng = [(i * 2654435761 % 1000) / 1000.0 for i in range(n)]
    X = np.zeros((n, len(PREDICTOR_FEATURES)))
    X[:, 0] = np.array(rng)
    for j in range(1, len(PREDICTOR_FEATURES)):
        X[:, j] = np.array([rng[(i + j * 7) % n] for i in range(n)])
    y = 0.05 * (X[:, 0] - 0.5) + np.array([((i * 40503 % 1000) / 1000.0 - 0.5) * 0.05
                                           for i in range(n)])
    meta = [(f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}", f"T{i%10}") for i in range(n)]
    base = [100.0] * n
    return {"X": X, "y": y, "meta": meta, "feature_names": list(PREDICTOR_FEATURES)}, base


def test_fit_any_estimator_and_evaluate():
    from sklearn.ensemble import GradientBoostingRegressor
    panel, base = _planted_panel()
    idx = list(range(len(panel["y"])))
    trained = H.fit(panel["X"], panel["y"],
                    GradientBoostingRegressor(n_estimators=40, max_depth=2, random_state=0))
    metrics = H.evaluate_at(trained, panel, base, idx)
    assert metrics["return"]["ic"] is not None
    assert "model_beats_naive_rmse" in metrics["price"]


def test_performance_log_records_required_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "PERF_LOG", tmp_path / "performance.log")
    ranges = {"train_range": ["2026-06-01", "2026-06-26"],
              "dev_range": ["2026-06-29", "2026-07-10"],
              "tickers": ["AAPL", "MSFT"], "features": ["tech.rsi14"],
              "label_strategy": "end_of_day_forward_return(H=1d)", "horizon_days": 1}
    metrics = {"return": {"ic": 0.04, "beats_null": True},
               "price": {"model": {"mape_pct": 2.0},
                         "naive_persistence": {"mape_pct": 2.1},
                         "model_beats_naive_rmse": True}}
    rec = H.log_performance("ridge", ranges, metrics, promoted=True)
    import json as _j
    logged = _j.loads((tmp_path / "performance.log").read_text().strip())
    # every required metadata field present
    for k in ("model", "label_strategy", "train_range", "dev_range", "tickers",
              "features", "dev_ic", "dev_beats_naive", "promoted"):
        assert k in logged
    assert logged["train_range"] == ["2026-06-01", "2026-06-26"]
    assert logged["dev_range"] == ["2026-06-29", "2026-07-10"]


def test_promotion_gate_rejects_no_edge():
    weak = {"return": {"ic": 0.001, "beats_null": False},
            "price": {"model_beats_naive_rmse": False}}
    assert H.meets_bar(weak) is False
    strong = {"return": {"ic": 0.05, "beats_null": True},
              "price": {"model_beats_naive_rmse": True}}
    assert H.meets_bar(strong) is True


def test_promote_register_load_predict_roundtrip(tmp_path, monkeypatch):
    from sklearn.linear_model import Ridge
    monkeypatch.setattr(H, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(W, "MODELS_DIR", tmp_path)

    panel, base = _planted_panel()
    idx = list(range(len(panel["y"])))
    trained = H.fit(panel["X"], panel["y"], Ridge(alpha=1.0))
    metrics = H.evaluate_at(trained, panel, base, idx)
    ranges = {"train_range": ["2026-06-01", "2026-06-26"],
              "dev_range": ["2026-06-29", "2026-07-10"],
              "tickers": ["T0"], "features": PREDICTOR_FEATURES,
              "label_strategy": "end_of_day_forward_return(H=1d)", "horizon_days": 1}
    H.promote("test_model", trained, ranges, metrics)
    # registered
    assert "test_model" in W.list_models()
    # loadable + predicts; feature 0 high -> higher prediction than feature 0 low
    p = W.Predictor.load("test_model")
    hi = p.predict({PREDICTOR_FEATURES[0]: 1.0})
    lo = p.predict({PREDICTOR_FEATURES[0]: 0.0})
    assert hi > lo
    # accepts a plain list too, and imputes missing dict keys
    assert isinstance(p.predict([0.5] * len(PREDICTOR_FEATURES)), float)
    assert isinstance(p.predict({}), float)


def test_missing_artifact_is_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "MODELS_DIR", tmp_path)
    mdir = tmp_path / "gone"
    mdir.mkdir()
    (mdir / "metadata.json").write_text('{"experiment": "modeling/x.py"}')
    with pytest.raises(FileNotFoundError, match="rebuild"):
        W.Predictor.load("gone")
