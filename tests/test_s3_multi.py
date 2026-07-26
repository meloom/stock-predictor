"""Tests for the multi-horizon walk-forward predictors (offline)."""
import numpy as np

import s3_multi


def test_walk_forward_is_leakage_free():
    """An OOS prediction at date d comes from a model trained only on dates < its block
    start — never on d's own future. Assert predictions only appear at/after warmup."""
    rng = np.random.RandomState(0)
    dates = [f"2026-01-{i:03d}" for i in range(1, 201)]
    rows = []
    for d in dates:
        for t in ("A", "B", "C"):
            x = rng.randn(len(s3_multi.PREDICTOR_FEATURES)).tolist()
            rows.append({"d": d, "t": t, "x": x,
                         "lab": {"ret_1d": float(x[0] * 0.01 + rng.randn() * 0.001)},
                         "close": 100.0})
    out = s3_multi._walk_reg(rows, "ret_1d")
    assert out, "walk-forward produced no predictions"
    warmup_date = dates[int(len(dates) * s3_multi.WARMUP_FRAC)]
    assert all(d >= warmup_date for (_, d) in out)


def test_reg_fit_predict_handle_all_nan_column():
    X = np.array([[1.0, np.nan, 3.0], [2.0, np.nan, 1.0], [3.0, np.nan, 2.0]])
    y = np.array([0.1, 0.2, 0.3])
    m = s3_multi._fit_reg(X, y)
    assert "sigma" in m and np.isfinite(m["sigma"])          # CI width computed
    assert np.isfinite(s3_multi._pred_reg(m, np.array([[1.5, np.nan, 2.0]]))).all()


def test_direction_classifier_gives_up_and_down():
    rng = np.random.RandomState(2)
    X = rng.randn(300, len(s3_multi.PREDICTOR_FEATURES))
    y = np.sign(X[:, 0]).astype(float)                       # classes {-1,0,1}-ish
    m = s3_multi._fit_clf(X, y)
    proba = s3_multi._pred_proba(m, X[:3])
    assert proba.shape[1] >= 2 and set(m["classes"]) <= {-1.0, 0.0, 1.0}


def test_naming_is_Nd_not_hN_and_covers_up_down_vol():
    names = {n for n, *_ in s3_multi.S3M_FEATURES}
    for h in (1, 5, 21):
        assert f"predict.ret_{h}d" in names
        assert f"predict.up_{h}d" in names and f"predict.down_{h}d" in names
        assert f"predict.ret_h{h}" not in names              # old naming gone
    assert "predict.vol_5d" in names and "predict.forecast" in names and "predict.band" in names
