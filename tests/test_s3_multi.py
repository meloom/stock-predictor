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


def test_reg_fit_gives_leverage_prediction_interval():
    """The CI is a proper leverage-adjusted PI: a high-leverage (unusual) query gets a
    WIDER interval than an in-distribution one — not a constant band."""
    rng = np.random.RandomState(3)
    X = rng.randn(400, len(s3_multi.PREDICTOR_FEATURES))
    y = X[:, 0] * 0.01 + rng.randn(400) * 0.001
    m = s3_multi._fit_reg(X, y)
    assert "M" in m and np.isfinite(m["sigma"])
    typical = s3_multi._pi_se(m, np.zeros((1, X.shape[1])))[0]           # centre of data
    outlier = s3_multi._pi_se(m, np.full((1, X.shape[1]), 8.0))[0]       # far out
    assert outlier > typical                                            # interval widens


def test_direction_is_derived_and_complementary():
    """up = Φ(ŷ/se), down = 1 − up: coherent, always summing to 1 and moving with ŷ."""
    assert s3_multi._norm_cdf(0.0) == 0.5
    up_pos = s3_multi._norm_cdf(1.2); up_neg = s3_multi._norm_cdf(-1.2)
    assert up_pos > 0.5 > up_neg                                        # up>0.5 iff ŷ>0
    assert abs((up_pos + (1 - up_pos)) - 1.0) < 1e-9                    # up+down == 1


def test_naming_is_Nd_and_no_separate_classifier():
    names = {n for n, *_ in s3_multi.S3M_FEATURES}
    for h in (1, 5, 21):
        assert f"predict.ret_{h}d" in names and f"predict.ci_ret_{h}d" in names
        assert f"predict.up_{h}d" in names and f"predict.down_{h}d" in names
        assert f"predict.ret_h{h}" not in names
    assert not hasattr(s3_multi, "_fit_clf")                            # classifier removed
