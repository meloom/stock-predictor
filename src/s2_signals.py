"""s2_signals.py — Stage S2: signal generation (DESIGN.md §S2), one file.

Reads S1 features from the store, derives technical / cross-sectional /
regime features, writes them back under the same bitemporal contract.
Sections: registry · pure computations · orchestrator.
Project style: one file per stage; simple, working. Tests: tests/test_s2_signals.py.

Hard rules encoded:
  - Point-in-time by construction: every input read goes through
    store.read_series(..., end_event_time=event_date), so a value from the
    future cannot enter a computation. This property is TESTED (appending
    future data must not change a past computation), not asserted.
  - Insufficient history → the feature is skipped and counted in metrics.
    No sentinel values, no padding (the predecessor's 999-style sentinels
    leaked into models as plausible-looking numbers).
"""
from __future__ import annotations

import math

from core import Trigger, FeatureStore, MARKET_SCOPE

# ═══════════════ Registry — the S2 feature set ═══════════════

S2_FEATURES = [
    # name, dtype, scope_kind, cadence, point-in-time rule
    ("tech.rsi14", "float", "ticker", "daily",
     "Wilder RSI over the 14 most recent closes with event_time <= the day."),
    ("tech.mom5", "float", "ticker", "daily",
     "close[t]/close[t-5] - 1, trailing trading days only."),
    ("tech.mom20", "float", "ticker", "daily",
     "close[t]/close[t-20] - 1, trailing trading days only."),
    ("tech.hvol20", "float", "ticker", "daily",
     "Stdev of the trailing 20 daily returns."),
    ("tech.vr20", "float", "ticker", "daily",
     "volume[t] / mean(volume over trailing 20 days)."),
    ("xsec.rank_rsi14", "float", "ticker", "daily",
     "Percentile rank of tech.rsi14 across the universe on the day (0..1)."),
    ("xsec.rank_mom5", "float", "ticker", "daily",
     "Percentile rank of tech.mom5 across the universe on the day (0..1)."),
    ("regime.breadth5", "float", "market", "daily",
     "Fraction of universe tickers with positive tech.mom5 on the day."),
]


def register_all(store: FeatureStore) -> None:
    for name, dtype, scope_kind, cadence, pit_rule in S2_FEATURES:
        store.register(name, dtype, scope_kind, source_stage="S2",
                       cadence=cadence, pit_rule=pit_rule)


# ═══════════════ Pure computations (lists in, float/None out) ═══════════════

def rsi14(closes: list[float]) -> float | None:
    """Wilder RSI; needs 15 closes (14 deltas)."""
    if len(closes) < 15:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - 14, len(closes))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def momentum(closes: list[float], days: int) -> float | None:
    if len(closes) < days + 1:
        return None
    return closes[-1] / closes[-1 - days] - 1.0


def hvol20(closes: list[float]) -> float | None:
    if len(closes) < 21:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - 20, len(closes))]
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def volume_ratio20(volumes: list[float]) -> float | None:
    if len(volumes) < 20:
        return None
    window = volumes[-20:]
    avg = sum(window) / len(window)
    return volumes[-1] / avg if avg > 0 else None


def pct_ranks(values: dict[str, float]) -> dict[str, float]:
    """{scope: value} -> {scope: percentile rank in 0..1}. Ties share the
    average rank; a single element ranks 0.5."""
    if not values:
        return {}
    if len(values) == 1:
        return {k: 0.5 for k in values}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_rank = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            out[items[k][0]] = avg_rank
        i = j + 1
    return out


# ═══════════════ Orchestrator ═══════════════

HISTORY_N = 30  # trading days of closes fetched per ticker (covers rsi14/mom20/hvol20)


def run_signal_generation(universe: list[str], event_date: str,
                          store: FeatureStore | None = None,
                          as_known_at: str | None = None) -> dict:
    """Derive S2 features for one event_date. All reads bounded by
    event_date (and optionally as_known_at) — lookahead-free by construction.
    Returns metrics (also logged to runs.jsonl with the trigger).
    """
    store = store or FeatureStore()
    register_all(store)

    with Trigger("signal_generation", stage="S2") as trig:
        rows: list[tuple] = []
        skipped: dict[str, int] = {}
        rsi_by_ticker: dict[str, float] = {}
        mom5_by_ticker: dict[str, float] = {}

        for t in universe:
            closes_series = store.read_series("price.close", t, event_date,
                                              HISTORY_N, as_known_at)
            vols_series = store.read_series("price.volume", t, event_date,
                                            HISTORY_N, as_known_at)
            # only compute if the ticker actually has a bar ON this date —
            # otherwise we'd silently compute "today's" signal from an old bar
            if not closes_series or closes_series[-1][0] != event_date:
                skipped["no_bar_on_date"] = skipped.get("no_bar_on_date", 0) + 1
                continue
            closes = [v for _, v in closes_series]
            vols = [v for _, v in vols_series]

            computed = {
                "tech.rsi14": rsi14(closes),
                "tech.mom5": momentum(closes, 5),
                "tech.mom20": momentum(closes, 20),
                "tech.hvol20": hvol20(closes),
                "tech.vr20": volume_ratio20(vols),
            }
            for feat, val in computed.items():
                if val is None:
                    skipped[feat] = skipped.get(feat, 0) + 1
                else:
                    rows.append((feat, t, event_date, round(val, 6)))
            if computed["tech.rsi14"] is not None:
                rsi_by_ticker[t] = computed["tech.rsi14"]
            if computed["tech.mom5"] is not None:
                mom5_by_ticker[t] = computed["tech.mom5"]

        # cross-sectional + regime (need the full universe pass first)
        for t, r in pct_ranks(rsi_by_ticker).items():
            rows.append(("xsec.rank_rsi14", t, event_date, round(r, 4)))
        for t, r in pct_ranks(mom5_by_ticker).items():
            rows.append(("xsec.rank_mom5", t, event_date, round(r, 4)))
        if mom5_by_ticker:
            breadth = sum(1 for v in mom5_by_ticker.values() if v > 0) / len(mom5_by_ticker)
            rows.append(("regime.breadth5", MARKET_SCOPE, event_date, round(breadth, 4)))

        store.write_many(rows, trigger_id=trig.trigger_id)
        trig.add_metrics(
            event_date=event_date,
            universe_size=len(universe),
            features_written=len(rows),
            skipped=skipped,
        )
        return {"trigger_id": trig.trigger_id, **trig.metrics}
