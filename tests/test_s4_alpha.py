"""S4 Alpha tests: regime scoring exactness, fail-toward-cash on missing
inputs, lineage stamps, deterministic event risk (UNKNOWN not LOW), and the
combine step that gates S3's prediction by regime + event veto."""
import pytest

import core as core_mod
from core import FeatureStore, MARKET_SCOPE
from s4_alpha import (compute_regime, classify_event_risk,
                      run_alpha, REGIME_THRESHOLD)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(core_mod, "LOGS_DIR", tmp_path / "logs")
    yield tmp_path


@pytest.fixture
def store(tmp_path):
    s = FeatureStore(tmp_path / "test.db")
    s.register("regime.breadth5", "float", "market", "S2", "daily", "pit")
    s.register("macro.vix", "float", "market", "S1", "daily", "pit")
    s.register("macro.spy_close", "float", "market", "S1", "daily", "pit")
    s.register("calendar.days_to_earnings", "int", "ticker", "S1", "daily", "pit")
    return s


def _seed_market(store, breadth=0.8, vix=15.0, spy_closes=None, last_day="2026-06-30",
                 ingested_at=None):
    spy_closes = spy_closes or [700.0] * 20
    days = [f"2026-06-{i+1:02d}" for i in range(len(spy_closes))]
    rows = [("macro.spy_close", MARKET_SCOPE, d, c) for d, c in zip(days, spy_closes)]
    rows.append(("regime.breadth5", MARKET_SCOPE, last_day, breadth))
    rows.append(("macro.vix", MARKET_SCOPE, last_day, vix))
    store.write_many(rows, trigger_id="seed", ingested_at=ingested_at)


def test_regime_score_exact_bullish(store):
    # breadth .8 -> .8*.4 = .32 | vix 15 -> 1.0*.3 = .30 | flat SPY -> dev 0 ->
    # (0+.02)/.04 = .5 -> .15  => score .77 -> TRADE
    _seed_market(store, breadth=0.8, vix=15.0)
    r = compute_regime(store, "2026-06-30")
    assert r["score"] == pytest.approx(0.77)
    assert r["decision"] == "TRADE"
    assert r["components"]["spy_trend"] == pytest.approx(0.5)


def test_regime_score_exact_bearish(store):
    # breadth .2 -> .08 | vix 30 -> 0 | SPY 2% below sma20 -> 0  => .08 -> CASH
    closes = [700.0] * 19 + [700.0 * 0.98 * 20 - 700.0 * 19]  # engineer last close ~2% under mean
    # simpler: last close such that dev <= -2%: use declining tail
    closes = [710.0] * 19 + [680.0]
    _seed_market(store, breadth=0.2, vix=30.0, spy_closes=closes)
    r = compute_regime(store, "2026-06-30")
    assert r["decision"] == "CASH"
    assert r["components"]["vix"] == 0.0
    assert r["components"]["spy_trend"] == 0.0
    assert r["score"] == pytest.approx(0.08)


def test_missing_inputs_fail_toward_cash(store):
    # nothing seeded at all
    r = compute_regime(store, "2026-06-30")
    assert r["decision"] == "CASH"
    assert r["score"] is None
    assert "missing inputs" in r["reason"]
    # partially seeded (no vix) still fails toward cash and names the gap
    _seed_market(store, breadth=0.5, vix=20.0)
    store2 = store  # vix present now; drop spy history instead by using early date
    r2 = compute_regime(store2, "2026-06-05")  # only 5 spy days exist by then
    assert r2["decision"] == "CASH"
    assert "spy_close" in r2["reason"]


def test_lineage_stamp_is_max_ingested_at(store):
    _seed_market(store, ingested_at="2026-06-30T21:00:00+00:00")
    # breadth arrives later than the rest
    store.write("regime.breadth5", MARKET_SCOPE, "2026-06-30", 0.9,
                trigger_id="late", ingested_at="2026-06-30T23:30:00+00:00")
    r = compute_regime(store, "2026-06-30")
    assert r["inputs_max_ingested_at"] == "2026-06-30T23:30:00+00:00"


def test_pit_future_data_does_not_change_past_decision(store):
    _seed_market(store)
    before = compute_regime(store, "2026-06-30")
    store.write_many([("macro.spy_close", MARKET_SCOPE, "2026-07-01", 100.0),
                      ("macro.vix", MARKET_SCOPE, "2026-07-01", 80.0),
                      ("regime.breadth5", MARKET_SCOPE, "2026-07-01", 0.0)],
                     trigger_id="future")
    after = compute_regime(store, "2026-06-30")
    assert before == after


def test_event_risk_deterministic_and_unknown(store):
    assert classify_event_risk(0) == "HIGH"
    assert classify_event_risk(2) == "HIGH"
    assert classify_event_risk(3) == "MEDIUM"
    assert classify_event_risk(5) == "MEDIUM"
    assert classify_event_risk(6) == "LOW"
    assert classify_event_risk(None) == "UNKNOWN"  # never silently LOW


def test_run_alpha_combines_prediction_regime_event(store):
    _seed_market(store)  # bullish -> regime TRADE
    store.register("predict.eod_return", "float", "ticker", "S3", "daily", "pit")
    # GOOD: positive prediction, no event risk -> actionable
    store.write("calendar.days_to_earnings", "GOOD", "2026-06-30", 45, trigger_id="s")
    store.write("predict.eod_return", "GOOD", "2026-06-30", 0.02, trigger_id="s")
    # EARN: positive prediction BUT earnings in 1 day -> event veto, not actionable
    store.write("calendar.days_to_earnings", "EARN", "2026-06-30", 1, trigger_id="s")
    store.write("predict.eod_return", "EARN", "2026-06-30", 0.02, trigger_id="s")
    # NEG: negative prediction -> not actionable
    store.write("calendar.days_to_earnings", "NEG", "2026-06-30", 45, trigger_id="s")
    store.write("predict.eod_return", "NEG", "2026-06-30", -0.01, trigger_id="s")

    m = run_alpha(["GOOD", "EARN", "NEG"], "2026-06-30", store=store)
    assert m["regime_decision"] == "TRADE"
    assert m["has_predictions"] is True
    assert m["actionable_signals"] == 1  # only GOOD

    assert store.read_asof("alpha.signal", "GOOD", "2026-06-30")["value"]["actionable"] is True
    assert store.read_asof("alpha.signal", "EARN", "2026-06-30")["value"]["actionable"] is False
    assert store.read_asof("alpha.signal", "NEG", "2026-06-30")["value"]["actionable"] is False


def test_run_alpha_cash_regime_blocks_all(store):
    # bearish market -> regime CASH -> nothing actionable even with good predictions
    _seed_market(store, breadth=0.1, vix=30.0,
                 spy_closes=[710.0] * 19 + [680.0])
    store.register("predict.eod_return", "float", "ticker", "S3", "daily", "pit")
    store.write("calendar.days_to_earnings", "GOOD", "2026-06-30", 45, trigger_id="s")
    store.write("predict.eod_return", "GOOD", "2026-06-30", 0.05, trigger_id="s")
    m = run_alpha(["GOOD"], "2026-06-30", store=store)
    assert m["regime_decision"] == "CASH"
    assert m["actionable_signals"] == 0


def test_run_alpha_no_predictions_still_runs(store):
    _seed_market(store)
    store.write("calendar.days_to_earnings", "AAPL", "2026-06-30", 45, trigger_id="s")
    m = run_alpha(["AAPL"], "2026-06-30", store=store)
    assert m["has_predictions"] is False
    assert m["actionable_signals"] == 0
    sig = store.read_asof("alpha.signal", "AAPL", "2026-06-30")["value"]
    assert sig["predicted_return"] is None and sig["actionable"] is False
