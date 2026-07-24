"""Trigger logging + S1 orchestration tests — fully offline via injected fetchers."""
import json

import pytest

import core as trigger_mod
from core import Trigger
from core import FeatureStore
from s1_data import run_daily_ingestion


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Point all runtime logs at a temp dir for every test."""
    monkeypatch.setattr(trigger_mod, "LOGS_DIR", tmp_path / "logs")
    yield tmp_path


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_run_logged_on_success(isolated_runtime):
    with Trigger("unit_test", stage="S1") as t:
        t.add_metrics(foo=1)
    runs = _read_jsonl(isolated_runtime / "logs" / "runs.jsonl")
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["metrics"] == {"foo": 1}


def test_run_logged_on_crash_and_exception_propagates(isolated_runtime):
    """Silence must never be indistinguishable from success."""
    with pytest.raises(RuntimeError):
        with Trigger("unit_test", stage="S1"):
            raise RuntimeError("boom")
    runs = _read_jsonl(isolated_runtime / "logs" / "runs.jsonl")
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert "boom" in runs[0]["error"]


def test_all_costs_attribute_to_one_trigger(isolated_runtime):
    """The user-specified semantics: one trigger initiating two LLM calls
    gets BOTH calls' costs — no orphan spend."""
    with Trigger("earnings_check", stage="S1") as t:
        t.record_cost("claude-sonnet", tokens_in=1000, tokens_out=500, web_searches=2)
        t.record_cost("claude-sonnet", tokens_in=2000, tokens_out=800, web_searches=1)
    ledger = _read_jsonl(isolated_runtime / "logs" / "cost_ledger.jsonl")
    assert len(ledger) == 2
    assert len({r["trigger_id"] for r in ledger}) == 1  # same trigger on both
    runs = _read_jsonl(isolated_runtime / "logs" / "runs.jsonl")
    total = sum(r["unit_cost"] for r in ledger)
    assert runs[0]["cost_usd"] == pytest.approx(total, abs=1e-6)


def test_ingestion_offline_end_to_end(isolated_runtime, tmp_path):
    store = FeatureStore(tmp_path / "features.db")

    def fake_bars(tickers):
        return {t: [{"date": "2026-07-24", "close": 100.0, "volume": 1e6}]
                for t in tickers if t != "DEAD"} | {"DEAD": []}

    def fake_macro():
        return {"vix": [{"date": "2026-07-24", "value": 18.5}],
                "yield10y": [{"date": "2026-07-24", "value": 4.6}],
                "spy_close": [{"date": "2026-07-23", "value": 740.0},
                              {"date": "2026-07-24", "value": 738.0}]}

    def fake_dte(ticker, asof=None):
        return None if ticker == "DEAD" else 30  # None stays None, no sentinels

    def fake_quote(ticker):
        # AAPL trades post-market at a price different from its close (100.0);
        # MSFT quote missing; DEAD absent
        return {"AAPL": {"price": 103.0, "session": "post"},
                "MSFT": None, "DEAD": None}.get(ticker)

    def fake_signal(ticker, trig):
        trig.record_cost("claude-sonnet", tokens_in=500, tokens_out=200, web_searches=1)
        return {"ticker": ticker, "has_recent_report": True,
                "report_date": "2026-07-23", "net_signal": "bullish"}

    metrics = run_daily_ingestion(
        universe=["AAPL", "MSFT", "DEAD"], store=store,
        fetch_bars=fake_bars, fetch_macro=fake_macro, fetch_dte=fake_dte,
        fetch_quote=fake_quote,
        earnings_signal_fn=fake_signal, earnings_check_tickers=["AAPL"])

    # coverage metrics are honest about the dead ticker
    assert metrics["tickers_with_bars"] == 2
    assert metrics["calendar_unknown"] == 1
    assert metrics["earnings_signals_written"] == 1

    # LIVE current price stored, session-aware, DISTINCT from the close
    assert metrics["current_prices_ok"] == 1
    assert metrics["current_price_sessions"] == {"post": 1}
    cur = store.read_asof("price.current", "AAPL", "2026-07-24")["value"]
    assert cur == {"price": 103.0, "session": "post"}     # the real current price
    assert store.read_asof("price.close", "AAPL", "2026-07-24")["value"] == 100.0  # NOT this
    assert store.read_asof("price.current", "MSFT", "2026-07-24") is None  # no fake fallback

    # store contents + lineage
    rec = store.read_asof("price.close", "AAPL", "2026-07-24")
    assert rec["value"] == 100.0
    assert rec["trigger_id"] == metrics["trigger_id"]
    sig = store.read_asof("fundamental.earnings_signal", "AAPL", "2026-07-24")
    assert sig["value"]["net_signal"] == "bullish"
    assert sig["event_time"] == "2026-07-23"  # event_time = report date

    # the LLM cost inside ingestion attributed to the ingestion trigger
    ledger = _read_jsonl(isolated_runtime / "logs" / "cost_ledger.jsonl")
    assert len(ledger) == 1
    assert ledger[0]["trigger_id"] == metrics["trigger_id"]


def test_fundamentals_publication_date_prevents_lookahead(isolated_runtime, tmp_path):
    """The make-or-break fundamentals rule: a statement is stored at its
    PUBLICATION date, not the fiscal period end — so a backtest cannot see it
    before it was filed."""
    from core import FeatureStore
    store = FeatureStore(tmp_path / "f.db")

    def fake_bars(tickers):
        return {t: [{"date": "2026-07-24", "close": 100.0, "volume": 1e6}] for t in tickers}

    def fake_statements(ticker, asof=None):
        # Q ending 2026-03-31 but ANNOUNCED 2026-05-01 (5 weeks later)
        return {"period_end": "2026-03-31", "event_time": "2026-05-01",
                "publish_source": "announcement", "revenue": 5e9,
                "net_income": 1e9, "total_equity": 2e10}

    metrics = run_daily_ingestion(
        universe=["AAPL"], store=store,
        fetch_bars=fake_bars, fetch_macro=lambda: {},
        fetch_dte=lambda t, asof=None: None, fetch_quote=lambda t: None,
        fetch_shares=lambda t: 1.5e9,
        fetch_statements=fake_statements,
        fetch_analyst=lambda t: {"forward_eps": 6.5, "n_analysts": 30})

    assert metrics["shares_ok"] == 1
    assert metrics["statements_ok"] == 1
    assert metrics["analyst_snapshots_ok"] == 1

    # stored at the ANNOUNCEMENT date, not the period end
    st = store.read_asof("fundamental.statements", "AAPL", "2026-12-31")
    assert st["event_time"] == "2026-05-01"
    assert st["value"]["period_end"] == "2026-03-31"

    # THE lookahead guard: a backtest standing on 2026-04-15 (after quarter
    # end, before the filing) must NOT see it
    assert store.read_asof("fundamental.statements", "AAPL", "2026-04-15") is None
    # but on 2026-05-02 (after filing) it IS visible
    assert store.read_asof("fundamental.statements", "AAPL", "2026-05-02") is not None

    assert store.read_asof("fundamental.shares_outstanding", "AAPL", "2026-07-24")["value"] == 1.5e9
    assert store.read_asof("fundamental.analyst_snapshot", "AAPL", "2026-07-24")["value"]["n_analysts"] == 30
