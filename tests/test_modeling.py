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
    split = H.purged_split(panel["meta"])
    trained = H.fit(panel["X"][split["train_idx"]], panel["y"][split["train_idx"]],
                    GradientBoostingRegressor(n_estimators=40, max_depth=2, random_state=0))
    metrics = H.evaluate_all(trained, panel, base, split)
    assert metrics["return"]["ic"] is not None
    assert "model_beats_naive_rmse" in metrics["price"]


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
    split = H.purged_split(panel["meta"])
    trained = H.fit(panel["X"][split["train_idx"]], panel["y"][split["train_idx"]],
                    Ridge(alpha=1.0))
    metrics = H.evaluate_all(trained, panel, base, split)

    H.promote("test_model", trained,
              {"experiment": "unit-test", "test_metrics": metrics})
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
