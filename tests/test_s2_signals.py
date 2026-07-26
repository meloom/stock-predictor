"""S2 tests: exact values on synthetic series, the point-in-time guarantee
(future data cannot change past computations), skip-not-sentinel policy,
cross-sectional correctness, lineage."""
import math

import pytest

import core as core_mod
from core import FeatureStore, MARKET_SCOPE
from s2_signals import (rsi14, momentum, hvol20, volume_ratio20, pct_ranks,
                        fundamental_ratios, xhorizon_features, run_signal_generation,
                        intraday_features)


def test_intraday_features_use_hourly_granularity():
    """Intraday features come from the HOURLY bars, not the daily EOD close."""
    bd = {"2026-07-23": [(98, 99), (99, 100), (100, 100)],       # prev session, closes 100
          "2026-07-24": [(102, 103), (103, 104), (104, 105)]}    # gap +2%, drifts to 105
    r = intraday_features(bd)
    assert r["tech.overnight_gap"] == pytest.approx(102 / 100 - 1)   # 102 open vs 100 close
    assert r["tech.intraday_ret"] == pytest.approx(105 / 102 - 1)    # open->close of session
    assert r["tech.intraday_vol5"] is not None                      # within-session vol exists


def test_intraday_features_empty_is_none_not_zero():
    r = intraday_features({})
    assert r["tech.intraday_ret"] is None and r["tech.overnight_gap"] is None


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


def test_xhorizon_at_high_vs_off_high():
    # a steady climb: last close IS the trailing high -> flagged extended
    up = [100 + i for i in range(120)]
    f = xhorizon_features(up)
    assert f["xh.dist_hi252"] == pytest.approx(0.0)
    assert f["xh.new_high_flag"] == 1.0
    assert f["xh.above_hi_streak"] == 10.0            # capped at 10
    assert f["xh.ret_21d"] == pytest.approx(up[-1] / up[-22] - 1.0)
    # same run, then a sharp pullback -> off the high, streak resets, flag off
    off = up + [219 * 0.85, 219 * 0.84, 219 * 0.83]
    g = xhorizon_features(off)
    assert g["xh.dist_hi252"] < -0.1
    assert g["xh.new_high_flag"] == 0.0
    assert g["xh.above_hi_streak"] == 0.0


def test_xhorizon_short_history_is_none_not_sentinel():
    # under the 41-close minimum every extension feature is genuinely missing
    assert all(v is None for v in xhorizon_features([100, 101, 102, 103]).values())
    # the 6-month return stays None until 127 closes exist, even when others fill in
    mid = [100 + i for i in range(80)]
    f = xhorizon_features(mid)
    assert f["xh.ret_21d"] is not None and f["xh.ret_63d"] is not None
    assert f["xh.ret_126d"] is None


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


# ── fundamentals ────────────────────────────────────────────────────────────

def test_fundamental_ratios_exact():
    # price 100, shares 10 -> mcap 1000; equity 500 -> bvps 50 -> B/P 0.5
    r = fundamental_ratios(
        price=100.0, shares=10.0,
        statements={"total_equity": 500.0, "net_income": 50.0, "revenue": 200.0,
                    "gross_profit": 120.0, "total_assets": 800.0,
                    "free_cash_flow": 40.0},
        analyst={"trailing_eps": 5.0})
    assert r["fund.market_cap"] == 1000.0
    assert r["fund.book_to_price"] == pytest.approx(0.5)      # (500/10)/100
    assert r["fund.earnings_yield"] == pytest.approx(0.05)    # 5/100
    assert r["fund.fcf_yield"] == pytest.approx(0.04)         # 40/1000
    assert r["fund.roe"] == pytest.approx(0.10)               # 50/500
    assert r["fund.gross_profitability"] == pytest.approx(0.15)  # 120/800
    assert r["fund.net_margin"] == pytest.approx(0.25)        # 50/200


def test_fundamental_ratios_missing_inputs_are_none_not_zero():
    r = fundamental_ratios(price=100.0, shares=None, statements=None, analyst=None)
    assert r["fund.market_cap"] is None      # no shares -> no mcap, not 0
    assert r["fund.roe"] is None
    assert r["fund.earnings_yield"] is None
    # zero denominator also -> None, never a divide error or inf
    r2 = fundamental_ratios(price=100.0, shares=10.0,
                            statements={"total_equity": 0.0, "net_income": 5.0}, analyst={})
    assert r2["fund.roe"] is None


def test_fundamentals_in_pipeline_are_publication_date_aware(store):
    """A statement filed AFTER event_date must be invisible to S2 — no
    fundamentals lookahead."""
    store.register("fundamental.shares_outstanding", "float", "ticker", "S1", "daily", "pit")
    store.register("fundamental.statements", "json", "ticker", "S1", "event", "pit")
    store.register("fundamental.analyst_snapshot", "json", "ticker", "S1", "daily", "pit")

    last = _seed(store, "AAPL", [100 + i for i in range(25)])   # ends 2026-06-25
    store.write("fundamental.shares_outstanding", "AAPL", "2026-06-01", 10.0, trigger_id="s")
    store.write("fundamental.analyst_snapshot", "AAPL", "2026-06-01",
                {"trailing_eps": 5.0}, trigger_id="s")
    # statement PUBLISHED 2026-06-20 (before the 06-25 event date) -> visible
    store.write("fundamental.statements", "AAPL", "2026-06-20",
                {"total_equity": 500.0, "net_income": 50.0, "revenue": 200.0,
                 "gross_profit": 120.0, "total_assets": 800.0, "free_cash_flow": 40.0},
                trigger_id="s")

    run_signal_generation(["AAPL"], last, store=store)
    assert store.read_asof("fund.roe", "AAPL", last)["value"] == pytest.approx(0.10)
    assert store.read_asof("fund.market_cap", "AAPL", last)["value"] == pytest.approx(124 * 10)

    # now a NEWER statement filed AFTER the event date must NOT change the past
    store.write("fundamental.statements", "AAPL", "2026-07-15",
                {"total_equity": 999.0, "net_income": 999.0}, trigger_id="future")
    run_signal_generation(["AAPL"], last, store=store)   # recompute same past day
    assert store.read_asof("fund.roe", "AAPL", last)["value"] == pytest.approx(0.10)  # unchanged


def test_fundamental_xsec_ranks(store):
    store.register("fundamental.shares_outstanding", "float", "ticker", "S1", "daily", "pit")
    store.register("fundamental.statements", "json", "ticker", "S1", "event", "pit")
    store.register("fundamental.analyst_snapshot", "json", "ticker", "S1", "daily", "pit")

    # HIGHQ: ROE 0.20; LOWQ: ROE 0.05 -> HIGHQ ranks top
    for tk, ni in [("HIGHQ", 100.0), ("LOWQ", 25.0)]:
        last = _seed(store, tk, [100 + i for i in range(25)])
        store.write("fundamental.shares_outstanding", tk, "2026-06-01", 10.0, trigger_id="s")
        store.write("fundamental.statements", tk, "2026-06-20",
                    {"total_equity": 500.0, "net_income": ni}, trigger_id="s")
        store.write("fundamental.analyst_snapshot", tk, "2026-06-01",
                    {"trailing_eps": 5.0}, trigger_id="s")

    run_signal_generation(["HIGHQ", "LOWQ"], last, store=store)
    assert store.read_asof("xsec.rank_roe", "HIGHQ", last)["value"] == 1.0
    assert store.read_asof("xsec.rank_roe", "LOWQ", last)["value"] == 0.0


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
