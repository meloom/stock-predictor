"""The prediction trigger: production registry, leakage rejection, S2-compose flow."""
import numpy as np
import pytest

import serving
import s3_multi


@pytest.fixture
def promoted(tmp_path, monkeypatch):
    """Promote a tiny synthetic production model to temp paths (no real store)."""
    monkeypatch.setattr(serving, "PROD_PKL", tmp_path / "prod.pkl")
    monkeypatch.setattr(serving, "PROD_META", tmp_path / "prod.json")
    feats = list(serving.PREDICTOR_FEATURES)
    rng = np.random.RandomState(1)
    X = rng.randn(300, len(feats))
    models = {"ret_1d": s3_multi._fit_reg(X, X[:, 0] * 0.01 + rng.randn(300) * 0.001)}
    import pickle
    bundle = {"train_start": "2025-07-01", "train_end": "2026-03-31", "features": feats,
              "models": models, "horizons": [1], "vol_horizon": 5, "n_train_rows": 300}
    serving.PROD_PKL.write_bytes(pickle.dumps(bundle))
    return bundle


def test_rejects_asof_inside_training_window(promoted):
    r = serving.predict("AAPL", "2026-02-01")
    assert r["status"] == "REJECTED" and "training window" in r["reason"]


def test_rejects_asof_equal_to_train_end(promoted):
    assert serving.predict("AAPL", "2026-03-31")["status"] == "REJECTED"   # boundary is training


def test_no_production_model_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(serving, "PROD_PKL", tmp_path / "none.pkl")
    assert serving.predict("AAPL", "2026-05-01")["status"] == "NO_PRODUCTION_MODEL"


def test_oos_asof_passes_the_leakage_gate(promoted, monkeypatch):
    """A post-training asof is NOT rejected (it reaches the data/compose stage)."""
    # stub S2-ready + a minimal store so we don't touch the real DB
    monkeypatch.setattr(serving, "_s2_ready", lambda asof, db_path=None: True)

    class _Store:
        def read_asof(self, feat, tk, asof, *a):
            return {"value": 100.0} if feat == "price.close" else {"value": 0.5}
    r = serving.predict("AAPL", "2026-05-01", store=_Store(), trigger_s2=False)
    assert r["status"] == "OK" and "1d" in r["predictions"]
    p = r["predictions"]["1d"]
    assert p["pred_price"] is not None
    assert p["ci_low"] <= p["pred_return"] <= p["ci_high"]        # confidence interval
