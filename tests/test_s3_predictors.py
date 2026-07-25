"""S3 Predictors tests: the model must actually predict (recover a planted
signal), must NOT claim edge on noise, panel assembly is PIT-correct, and
live prediction writes lineage-stamped outputs."""
import numpy as np
import pytest

import core as core_mod
from core import FeatureStore, MARKET_SCOPE
from s3_predictors import (assemble_panel, train, train_classifier,
                           train_dual_classifier, evaluate,
                           evaluate_price, run_predictors, predict_eod,
                           predict_proba_eod, PREDICTOR_FEATURES)


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


def test_classifier_recovers_direction():
    """The live big-move classifier must learn direction: plant a feature that
    drives big up-moves, confirm predict_proba assigns high p_up to up cases."""
    g = np.random.default_rng(3)
    n = 3000
    X = g.standard_normal((n, len(PREDICTOR_FEATURES)))
    # feature 0 large -> big positive return; feature 0 very negative -> big drop
    y = 0.06 * X[:, 0] + g.standard_normal(n) * 0.01
    cut = int(n * 0.7)
    trained = train_classifier(X[:cut], y[:cut], move=0.03)
    assert trained["kind"] == "classifier" and set(trained["classes"]) <= {-1, 0, 1}
    # standardize + score the held-out set directly through the model
    Xz = np.nan_to_num((X[cut:] - trained["mean"]) / trained["std"])
    proba = trained["model"].predict_proba(Xz)
    iu = trained["classes"].index(1)
    p_up = proba[:, iu]
    yb = y[cut:]
    # p_up must be higher on the actual big-up rows than on the big-down rows
    assert p_up[yb > 0.03].mean() > p_up[yb < -0.03].mean() + 0.2


def test_run_predictors_classifier_writes_probabilities(store):
    days = [f"2026-06-{i:02d}" for i in range(1, 11)]
    for i, d in enumerate(days):
        for tk, base in [("AAPL", 100.0), ("MSFT", 200.0)]:
            store.write("price.close", tk, d, base + i, trigger_id="s")
            store.write("tech.rsi14", tk, d, 50.0, trigger_id="s")
    panel = assemble_panel(store, ["AAPL", "MSFT"], days[:8], horizon_days=1)
    clf = train_classifier(panel["X"], panel["y"], move=0.001)  # tiny move -> non-degenerate labels

    m = run_predictors(["AAPL", "MSFT"], "2026-06-05", store=store, trained=clf)
    assert m["status"] == "PREDICTED"
    for feat in ("predict.p_up", "predict.p_down", "predict.confidence", "predict.direction"):
        rec = store.read_asof(feat, "AAPL", "2026-06-05")
        assert rec is not None and isinstance(rec["value"], float)
    # S4 back-compat: predict.eod_return is still emitted
    assert store.read_asof("predict.eod_return", "AAPL", "2026-06-05") is not None
    meta = store.read_asof("predict.eod_meta", MARKET_SCOPE, "2026-06-05")
    assert meta["value"]["model"] == "big_move_classifier/histgbm"


def test_dual_classifier_uses_logistic_up_histgbm_down():
    """The side-specific dual model must take p_up from the logistic head and
    p_down from the histgbm head, and recover a planted directional signal."""
    g = np.random.default_rng(5)
    n = 3000
    X = g.standard_normal((n, len(PREDICTOR_FEATURES)))
    y = 0.06 * X[:, 0] + g.standard_normal(n) * 0.01
    trained = train_dual_classifier(X[:2000], y[:2000], move=0.03)
    assert trained["kind"] == "dual_classifier"
    assert type(trained["up_model"]).__name__ == "LogisticRegression"
    assert type(trained["dn_model"]).__name__ == "HistGradientBoostingClassifier"
    # p_up should be higher on true up rows than true down rows (feature-0 driven)
    Xz = np.nan_to_num((X[2000:] - trained["mean"]) / trained["std"])
    iu = trained["up_classes"].index(1)
    p_up = trained["up_model"].predict_proba(Xz)[:, iu]
    yb = y[2000:]
    assert p_up[yb > 0.03].mean() > p_up[yb < -0.03].mean() + 0.2


def test_run_predictors_dual_writes_probabilities(store):
    days = [f"2026-06-{i:02d}" for i in range(1, 11)]
    # two-directional prices so labels span +1/-1/0 (logistic needs >=2 classes)
    px = {"AAPL": [100, 103, 101, 104, 102, 105, 103, 106, 104, 107],
          "MSFT": [200, 196, 199, 195, 198, 194, 197, 193, 196, 192]}
    for i, d in enumerate(days):
        for tk in ("AAPL", "MSFT"):
            store.write("price.close", tk, d, float(px[tk][i]), trigger_id="s")
            store.write("tech.rsi14", tk, d, 50.0, trigger_id="s")
    panel = assemble_panel(store, ["AAPL", "MSFT"], days[:8], horizon_days=1)
    dual = train_dual_classifier(panel["X"], panel["y"], move=0.001)
    m = run_predictors(["AAPL", "MSFT"], "2026-06-05", store=store, trained=dual)
    assert m["status"] == "PREDICTED"
    assert store.read_asof("predict.p_up", "AAPL", "2026-06-05") is not None
    assert store.read_asof("predict.eod_return", "AAPL", "2026-06-05") is not None  # S4 compat
    meta = store.read_asof("predict.eod_meta", MARKET_SCOPE, "2026-06-05")
    assert meta["value"]["model"] == "dual: logistic(up)+histgbm(down)"
