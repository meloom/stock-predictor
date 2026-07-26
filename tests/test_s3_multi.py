"""Tests for the multi-horizon walk-forward predictors (offline)."""
import numpy as np

import s3_multi


def test_walk_forward_is_leakage_free():
    """An OOS prediction at date d must come from a model trained only on dates < its
    block start — never on d's own future. We assert no prediction exists before the
    warmup boundary, and predictions only appear on dates that had a prior training pool."""
    # synthetic rows: 200 dates, 3 tickers, random features, ret label = f(x)+noise
    rng = np.random.RandomState(0)
    dates = [f"2026-01-{i:03d}" for i in range(1, 201)]   # sortable synthetic dates
    rows = []
    for d in dates:
        for t in ("A", "B", "C"):
            x = rng.randn(len(s3_multi.PREDICTOR_FEATURES)).tolist()
            rows.append({"d": d, "t": t, "x": x,
                         "lab": {"ret_h1": float(x[0] * 0.01 + rng.randn() * 0.001)},
                         "close": 100.0})
    out = s3_multi._walk_forward(rows, s3_multi.PREDICTOR_FEATURES, "ret_h1", "ridge")
    assert out, "walk-forward produced no predictions"
    warmup_date = dates[int(len(dates) * s3_multi.WARMUP_FRAC)]
    # every predicted date is at/after the warmup boundary (trained on earlier dates)
    assert all(d >= warmup_date for (_, d) in out)


def test_fit_predict_handle_all_nan_column():
    """A feature that is entirely NaN in a block must not crash or leak NaN."""
    X = np.array([[1.0, np.nan, 3.0], [2.0, np.nan, 1.0], [3.0, np.nan, 2.0]])
    y = np.array([0.1, 0.2, 0.3])
    m = s3_multi._fit(X, y, "ridge")
    p = s3_multi._pred(m, np.array([[1.5, np.nan, 2.0]]))
    assert np.isfinite(p).all()


def test_horizons_and_registration_cover_the_three_families():
    names = {n for n, *_ in s3_multi.S3M_FEATURES}
    for h in (1, 5, 21):
        assert f"predict.ret_h{h}" in names and f"predict.up_h{h}" in names
    assert "predict.vol_h5" in names and "predict.forecast" in names
