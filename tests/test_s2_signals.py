"""S2 tests: exact values on synthetic series, the point-in-time guarantee
(future data cannot change past computations), skip-not-sentinel policy,
cross-sectional correctness, lineage."""
import math

import pytest

import core as core_mod
from core import FeatureStore, MARKET_SCOPE
from s2_signals import (rsi14, momentum, hvol20, volume_ratio20, pct_ranks,
                        run_signal_generation)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(core_mod, "LOGS_DIR", tmp_path / "logs")
    yield tmp_path


@pytest.fixture
def store(tmp_path):
    s = FeatureStore(tmp_path / "test.db")
    s.register("price.close", "float", "ticker", "S1", "daily", "post-close")
    s.register("price.volume", "float", "ticker", "S1", "daily", "post-close")
    return s


def _seed(store, ticker, closes, start_day=1, volume=1e6, trigger_id="seed"):
    rows = []
    for i, c in enumerate(closes):
        day = f"2026-06-{start_day + i:02d}"
        rows.append(("price.close", ticker, day, c))
        rows.append(("price.volume", ticker, day, volume))
    store.write_many(rows, trigger_id=trigger_id)
    return f"2026-06-{start_day + len(closes) - 1:02d}"


# ── pure computations ──────────────────────────────────────────────────────

def test_momentum_exact():
    closes = [100, 100, 100, 100, 100, 100, 110]
    assert momentum(closes, 5) == pytest.approx(0.10)
    assert momentum(closes, 20) is None  # insufficient history -> None, no sentinel


def test_rsi_extremes_and_known_value():
    up = list(range(100, 116))            # monotonic gains
    assert rsi14(up) == 100.0
    down = list(range(116, 100, -1))      # monotonic losses
    assert rsi14(down) == pytest.approx(0.0)
    assert rsi14([100] * 14) is None      # 14 closes = 13 deltas: not enough
    # alternating +2/-1: avg_gain=1, avg_loss=0.5, RS=2, RSI=66.67
    alt = [100]
    for i in range(14):
        alt.append(alt[-1] + (2 if i % 2 == 0 else -1))
    assert rsi14(alt) == pytest.approx(66.6667, abs=0.01)


def test_hvol_and_vr():
    flat = [100.0] * 21
    assert hvol20(flat) == pytest.approx(0.0)
    assert hvol20([100.0] * 20) is None
    vols = [1e6] * 19 + [2e6]
    expected_avg = (19 * 1e6 + 2e6) / 20
    assert volume_ratio20(vols) == pytest.approx(2e6 / expected_avg)


def test_pct_ranks_with_ties():
    r = pct_ranks({"A": 1.0, "B": 2.0, "C": 2.0, "D": 3.0})
    assert r["A"] == 0.0 and r["D"] == 1.0
    assert r["B"] == r["C"] == pytest.approx(0.5)
    assert pct_ranks({"only": 5.0}) == {"only": 0.5}


# ── the point-in-time guarantee ────────────────────────────────────────────

def test_future_data_cannot_change_past_computation(store):
    """THE S2 property: signals for day T computed before and after future
    days exist must be identical."""
    closes = [100 + i for i in range(25)]
    last_day = _seed(store, "AAPL", closes)

    m1 = run_signal_generation(["AAPL"], last_day, store=store)
    v1 = store.read_asof("tech.mom5", "AAPL", last_day)["value"]
    rsi_1 = store.read_asof("tech.rsi14", "AAPL", last_day)["value"]

    # future days arrive (much higher closes — would distort anything leaky)
    store.write_many([("price.close", "AAPL", "2026-06-27", 500.0),
                      ("price.close", "AAPL", "2026-06-28", 600.0)],
                     trigger_id="future")

    run_signal_generation(["AAPL"], last_day, store=store)  # recompute same day
    assert store.read_asof("tech.mom5", "AAPL", last_day)["value"] == v1
    assert store.read_asof("tech.rsi14", "AAPL", last_day)["value"] == rsi_1
    assert m1["features_written"] > 0


def test_no_bar_on_date_skips_not_stale(store):
    """A ticker with no bar ON the event date is skipped — never silently
    computed from an old bar (that would be a stale signal presented as fresh)."""
    _seed(store, "AAPL", [100 + i for i in range(25)])       # ends 06-25
    metrics = run_signal_generation(["AAPL"], "2026-06-26", store=store)
    assert metrics["skipped"] == {"no_bar_on_date": 1}
    assert metrics["features_written"] == 0


def test_insufficient_history_skips_that_feature_only(store):
    last_day = _seed(store, "AAPL", [100, 101, 102, 100, 99, 103, 105])  # 7 bars
    metrics = run_signal_generation(["AAPL"], last_day, store=store)
    assert store.read_asof("tech.mom5", "AAPL", last_day)["value"] == pytest.approx(105 / 101 - 1)
    assert store.read_asof("tech.rsi14", "AAPL", last_day) is None   # skipped
    assert metrics["skipped"]["tech.rsi14"] == 1


# ── cross-sectional + regime + lineage ─────────────────────────────────────

def test_xsec_and_breadth_and_lineage(store):
    up = [100 + i for i in range(25)]
    down = [124 - i for i in range(25)]
    _seed(store, "UP", up)
    last_day = _seed(store, "DOWN", down)

    metrics = run_signal_generation(["UP", "DOWN"], last_day, store=store)
    assert store.read_asof("xsec.rank_mom5", "UP", last_day)["value"] == 1.0
    assert store.read_asof("xsec.rank_mom5", "DOWN", last_day)["value"] == 0.0
    assert store.read_asof("regime.breadth5", MARKET_SCOPE, last_day)["value"] == 0.5

    out = store.outputs_of(metrics["trigger_id"])
    features = {f["feature"] for f in out["features"]}
    assert "tech.rsi14" in features and "regime.breadth5" in features
    assert out["total_values"] == metrics["features_written"]
