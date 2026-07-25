"""S3 Predictors tests: the model must actually predict (recover a planted
signal), must NOT claim edge on noise, panel assembly is PIT-correct, and
live prediction writes lineage-stamped outputs."""
import numpy as np
import pytest

import core as core_mod
from core import FeatureStore
from s3_predictors import (assemble_panel, train, evaluate, evaluate_price,
                           run_predictors, predict_eod, PREDICTOR_FEATURES)


def test_price_eval_vs_naive_baseline():
    # base 100; actual return +2% -> actual price 102.
    base = [100.0, 200.0, 50.0]
    actual = [0.02, -0.01, 0.04]
    # a model that predicts the actual return exactly -> zero error, beats naive
    perfect = evaluate_price(actual, actual, base)
    assert perfect["model"]["rmse"] == 0.0
    assert perfect["model_beats_naive_rmse"] is True
    # a model that predicts zero return == naive persistence -> NO improvement
    naive_like = evaluate_price([0.0, 0.0, 0.0], actual, base)
    assert naive_like["model"]["rmse"] == naive_like["naive_persistence"]["rmse"]
    assert naive_like["model_beats_naive_rmse"] is False
    assert abs(naive_like["rmse_improvement_pct"]) < 1e-9


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(core_mod, "LOGS_DIR", tmp_path / "logs")
    yield tmp_path


def test_recovers_planted_signal():
    """A model must predict: plant a feature that drives the target, confirm
    positive held-out IC + beats the null + the loaded feature dominates."""
    rng = [(i * 2654435761 % 1000) / 1000.0 for i in range(4000)]
    n = len(rng)
    g = np.random.default_rng(0)
    X = g.standard_normal((n, len(PREDICTOR_FEATURES)))
    noise = g.standard_normal(n) * 0.02
    y = 0.05 * X[:, 0] + noise                       # target driven by feature 0
    cut = int(n * 0.7)
    m = train(X[:cut], y[:cut])
    ev = evaluate(m, X[cut:], y[cut:])
    assert ev["ic"] is not None and ev["ic"] > 0.3, ev
    assert ev["beats_null"] is True
    assert m["coefficients"][PREDICTOR_FEATURES[0]] == max(m["coefficients"].values())


def test_no_edge_on_noise():
    """Pure noise -> no claimed edge (IC ~ 0). No overfitting-to-noise."""
    N = 3000
    g = np.random.default_rng(1)
    X = g.standard_normal((N, len(PREDICTOR_FEATURES)))
    y = g.standard_normal(N)                          # target unrelated to X
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
