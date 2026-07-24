"""S3 Predictors tests: the model must actually predict (recover a planted
signal), must NOT claim edge on noise, panel assembly is PIT-correct, and
live prediction writes lineage-stamped outputs."""
import numpy as np
import pytest

import core as core_mod
from core import FeatureStore
from s3_predictors import (assemble_panel, train, evaluate, run_predictors,
                           predict_eod, PREDICTOR_FEATURES)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(core_mod, "LOGS_DIR", tmp_path / "logs")
    yield tmp_path


def test_recovers_planted_signal():
    """A model must predict: plant a feature that drives the target, confirm
    positive held-out IC + beats the null + the loaded feature dominates."""
    rng = [(i * 2654435761 % 1000) / 1000.0 for i in range(4000)]
    n = len(rng)
    X = np.zeros((n, len(PREDICTOR_FEATURES)))
    X[:, 0] = np.array(rng)
    for j in range(1, len(PREDICTOR_FEATURES)):
        X[:, j] = np.array([rng[(i + j * 7) % n] for i in range(n)])
    noise = np.array([((i * 40503 % 1000) / 1000.0 - 0.5) * 0.1 for i in range(n)])
    y = 0.05 * (X[:, 0] - 0.5) + noise
    cut = int(n * 0.7)
    m = train(X[:cut], y[:cut])
    ev = evaluate(m, X[cut:], y[cut:])
    assert ev["ic"] is not None and ev["ic"] > 0.3, ev
    assert ev["beats_null"] is True
    assert m["coefficients"][PREDICTOR_FEATURES[0]] == max(m["coefficients"].values())


def test_no_edge_on_noise():
    """Pure noise -> no claimed edge (IC ~ 0). No overfitting-to-noise."""
    N = 3000
    X = np.array([[(i * (j + 3) * 2654435761 % 1000) / 1000.0
                   for j in range(len(PREDICTOR_FEATURES))] for i in range(N)], dtype=float)
    y = np.array([((i * 40503 % 1000) / 1000.0 - 0.5) for i in range(N)])
    cut = int(N * 0.7)
    ev = evaluate(train(X[:cut], y[:cut]), X[cut:], y[cut:])
    assert abs(ev["ic"]) < 0.15, ev


@pytest.fixture
def store(tmp_path):
    s = FeatureStore(tmp_path / "test.db")
    s.register("price.close", "float", "ticker", "S1", "daily", "pit")
    for f in PREDICTOR_FEATURES:
        s.register(f, "float", "ticker", "S2", "daily", "pit")
    return s


def test_panel_is_point_in_time(store):
    """assemble_panel must not leak the future: the forward return uses prices
    AFTER the date, and features are read as-known-at the date."""
    days = [f"2026-06-{i:02d}" for i in range(1, 11)]
    for i, d in enumerate(days):
        store.write("price.close", "AAPL", d, 100.0 + i, trigger_id="s")
        store.write("tech.rsi14", "AAPL", d, 50.0 + i, trigger_id="s")
        store.write("price.close", "MSFT", d, 200.0 - i, trigger_id="s")
        store.write("tech.rsi14", "MSFT", d, 50.0 - i, trigger_id="s")
    panel = assemble_panel(store, ["AAPL", "MSFT"], days[:5], horizon_days=1)
    # AAPL on 2026-06-01: close 100 -> next close 101 -> fwd_ret 0.01
    idx = panel["meta"].index(("2026-06-01", "AAPL"))
    assert panel["y"][idx] == pytest.approx(0.01)
    # feature read as-of that date (rsi 50, not a later value)
    assert panel["X"][idx][0] == pytest.approx(50.0)


def test_run_predictors_no_model(store):
    m = run_predictors(["AAPL"], "2026-06-05", store=store, trained=None)
    assert m["status"] == "NO_MODEL"


def test_run_predictors_writes_predictions(store):
    days = [f"2026-06-{i:02d}" for i in range(1, 11)]
    for i, d in enumerate(days):
        for tk, base in [("AAPL", 100.0), ("MSFT", 200.0)]:
            store.write("price.close", tk, d, base + i, trigger_id="s")
            store.write("tech.rsi14", tk, d, 50.0, trigger_id="s")
    panel = assemble_panel(store, ["AAPL", "MSFT"], days[:8], horizon_days=1)
    trained = train(panel["X"], panel["y"])

    m = run_predictors(["AAPL", "MSFT"], "2026-06-05", store=store, trained=trained)
    assert m["status"] == "PREDICTED" and m["predictions_written"] == 2
    r = store.read_asof("predict.eod_return", "AAPL", "2026-06-05")
    assert r is not None and isinstance(r["value"], float)
    px = store.read_asof("predict.eod_price", "AAPL", "2026-06-05")["value"]
    assert px is not None  # implied close = price * (1 + predicted return)
    assert store.outputs_of(m["trigger_id"])["total_values"] >= 4
