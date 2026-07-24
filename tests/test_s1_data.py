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

    def fake_signal(ticker, trig):
        trig.record_cost("claude-sonnet", tokens_in=500, tokens_out=200, web_searches=1)
        return {"ticker": ticker, "has_recent_report": True,
                "report_date": "2026-07-23", "net_signal": "bullish"}

    metrics = run_daily_ingestion(
        universe=["AAPL", "MSFT", "DEAD"], store=store,
        fetch_bars=fake_bars, fetch_macro=fake_macro, fetch_dte=fake_dte,
        earnings_signal_fn=fake_signal, earnings_check_tickers=["AAPL"])

    # coverage metrics are honest about the dead ticker
    assert metrics["tickers_with_bars"] == 2
    assert metrics["calendar_unknown"] == 1
    assert metrics["earnings_signals_written"] == 1

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
