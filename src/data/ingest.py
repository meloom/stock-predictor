"""S1 daily ingestion orchestrator.

Fetchers are injected (defaults = the real network fetchers in sources.py) so
the orchestration logic — registration, store writes, coverage/freshness
metrics, run logging — is fully unit-testable offline.

The run record this emits is exactly what S8's audit consumes:
  metrics: {universe_size, tickers_with_bars, coverage_pct, macro_series,
            calendar_ok, calendar_unknown, earnings_signals_written}
plus, via the store itself, per-feature freshness (latest event_time /
ingested_at) and per-write trigger_id lineage.
"""
from __future__ import annotations

from datetime import datetime, timezone

from common.trigger import Trigger
from feature_store.store import FeatureStore, MARKET_SCOPE
from data.registry import register_all
from data import sources


def run_daily_ingestion(universe: list[str],
                        store: FeatureStore | None = None,
                        fetch_bars=sources.fetch_daily_bars,
                        fetch_macro=sources.fetch_macro,
                        fetch_dte=sources.fetch_days_to_earnings,
                        earnings_signal_fn=None,
                        earnings_check_tickers: list[str] | None = None) -> dict:
    """One full S1 pass. Returns the metrics dict (also logged to runs.jsonl).

    earnings_signal_fn: optional callable(ticker, trigger) -> signal dict.
    earnings_check_tickers: subset to run LLM extraction for (cost control is
    explicit and visible here — never a silent skip; the metrics record how
    many were checked vs. skipped).
    """
    store = store or FeatureStore()
    register_all(store)

    with Trigger("daily_ingestion", stage="S1") as trig:
        today = datetime.now(timezone.utc).date().isoformat()

        # -- prices ----------------------------------------------------------
        bars = fetch_bars(universe)
        rows = []
        tickers_ok = 0
        for t, series in bars.items():
            if series:
                tickers_ok += 1
            for bar in series:
                rows.append(("price.close", t, bar["date"], bar["close"]))
                rows.append(("price.volume", t, bar["date"], bar["volume"]))
        store.write_many(rows, trigger_id=trig.trigger_id)

        # -- macro -----------------------------------------------------------
        macro = fetch_macro()
        macro_rows = [(f"macro.{k}", MARKET_SCOPE, today, v) for k, v in macro.items()]
        store.write_many(macro_rows, trigger_id=trig.trigger_id)

        # -- earnings calendar ----------------------------------------------
        cal_ok, cal_unknown = 0, 0
        for t in universe:
            dte = fetch_dte(t)
            if dte is None:
                cal_unknown += 1  # None stays None — no 999-style sentinels
                continue
            store.write("calendar.days_to_earnings", t, today, dte,
                        trigger_id=trig.trigger_id)
            cal_ok += 1

        # -- earnings signals (LLM, cost-attributed to this trigger) --------
        signals_written = 0
        check_list = earnings_check_tickers or []
        if earnings_signal_fn is not None:
            for t in check_list:
                sig = earnings_signal_fn(t, trig)
                if sig.get("has_recent_report") and sig.get("report_date"):
                    store.write("fundamental.earnings_signal", t,
                                sig["report_date"], sig,
                                trigger_id=trig.trigger_id)
                    signals_written += 1

        coverage = tickers_ok / len(universe) * 100 if universe else 0.0
        trig.add_metrics(
            universe_size=len(universe),
            tickers_with_bars=tickers_ok,
            coverage_pct=round(coverage, 1),
            macro_series=sorted(macro.keys()),
            calendar_ok=cal_ok,
            calendar_unknown=cal_unknown,
            earnings_checked=len(check_list),
            earnings_signals_written=signals_written,
        )
        return {"trigger_id": trig.trigger_id, **trig.metrics}
