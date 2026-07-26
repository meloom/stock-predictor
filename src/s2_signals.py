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
    # -- lagged daily RETURNS (not price levels): the last 7 daily % moves.
    #    ret_lagK = close[t-K+1]/close[t-K] - 1. PIT-safe (each uses only closes
    #    <= t). Captures short-term momentum/reversal the aggregates miss. --
    ("tech.ret_lag1", "float", "ticker", "daily", "daily return realized on day t (most recent)."),
    ("tech.ret_lag2", "float", "ticker", "daily", "daily return on day t-1."),
    ("tech.ret_lag3", "float", "ticker", "daily", "daily return on day t-2."),
    ("tech.ret_lag4", "float", "ticker", "daily", "daily return on day t-3."),
    ("tech.ret_lag5", "float", "ticker", "daily", "daily return on day t-4."),
    ("tech.ret_lag6", "float", "ticker", "daily", "daily return on day t-5."),
    ("tech.ret_lag7", "float", "ticker", "daily", "daily return on day t-6."),
    # -- INTRADAY granularity (from the HOURLY bars, not the daily EOD projection).
    #    The daily close throws away within-session structure; these keep it. --
    ("tech.intraday_ret", "float", "ticker", "daily",
     "(last hourly close / first hourly open) - 1 on the session — intraday drift."),
    ("tech.overnight_gap", "float", "ticker", "daily",
     "(today's first hourly open / prior session's last hourly close) - 1 — the gap."),
    ("tech.intraday_vol5", "float", "ticker", "daily",
     "5-session mean of within-session hourly close-to-close return stdev — intraday vol."),
    # -- fundamentals (from S1's statements/shares/analyst; the enrichment that
    #    covers value/quality/size — categories the technicals above miss) --
    ("fund.market_cap", "float", "ticker", "daily",
     "price.close * shares_outstanding (size factor)."),
    ("fund.book_to_price", "float", "ticker", "daily",
     "(total_equity / shares) / price.close — VALUE (inverse P/B, ranks well "
     "even when negative)."),
    ("fund.earnings_yield", "float", "ticker", "daily",
     "trailing_eps / price.close — VALUE (inverse P/E)."),
    ("fund.fcf_yield", "float", "ticker", "daily",
     "free_cash_flow / market_cap — VALUE (quarterly-basis, cross-sectionally "
     "comparable)."),
    ("fund.roe", "float", "ticker", "daily",
     "net_income / total_equity — QUALITY (quarterly basis)."),
    ("fund.gross_profitability", "float", "ticker", "daily",
     "gross_profit / total_assets — QUALITY (Novy-Marx); strongest single "
     "quality signal in the literature."),
    ("fund.net_margin", "float", "ticker", "daily",
     "net_income / revenue — QUALITY."),
    ("xsec.rank_rsi14", "float", "ticker", "daily",
     "Percentile rank of tech.rsi14 across the universe on the day (0..1)."),
    ("xsec.rank_mom5", "float", "ticker", "daily",
     "Percentile rank of tech.mom5 across the universe on the day (0..1)."),
    ("xsec.rank_earnings_yield", "float", "ticker", "daily",
     "Cross-sectional percentile rank of fund.earnings_yield (0..1)."),
    ("xsec.rank_fcf_yield", "float", "ticker", "daily",
     "Cross-sectional percentile rank of fund.fcf_yield (0..1)."),
    ("xsec.rank_roe", "float", "ticker", "daily",
     "Cross-sectional percentile rank of fund.roe (0..1)."),
    ("xsec.rank_gross_profitability", "float", "ticker", "daily",
     "Cross-sectional percentile rank of fund.gross_profitability (0..1)."),
    # -- LONG-HORIZON extension (the champion block from grounded error analysis:
    #    see modeling/ERROR_ANALYSIS.md). The top confident-wrong LONG calls
    #    (MRVL/AMAT/MU 2026-06-30..07-01) were all names at a 52-week high after a
    #    multi-month parabolic run, then a 'sell-the-news' reversal. These measure
    #    that stretch; they lifted down-side per-day precision@1 from 2.5x to 2.9x
    #    the base rate. PIT-safe (trailing closes only). --
    ("xh.ret_21d", "float", "ticker", "daily", "close[t]/close[t-21] - 1 (1-month return)."),
    ("xh.ret_63d", "float", "ticker", "daily", "close[t]/close[t-63] - 1 (3-month return)."),
    ("xh.ret_126d", "float", "ticker", "daily", "close[t]/close[t-126] - 1 (6-month return)."),
    ("xh.dist_hi252", "float", "ticker", "daily",
     "(close - trailing 252d high)/high; <=0, 0 = at the 52-week high."),
    ("xh.new_high_flag", "float", "ticker", "daily", "1 if close within 2% of the trailing 252d high, else 0."),
    ("xh.above_hi_streak", "float", "ticker", "daily",
     "consecutive recent days (<=10) closing within 3% of the trailing high."),
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


def _div(a, b) -> float | None:
    """Safe divide: None if either side missing or denominator is 0 — no
    sentinels, a missing ratio is genuinely missing."""
    if a is None or b is None or b == 0:
        return None
    return a / b


def fundamental_ratios(price: float | None, shares: float | None,
                       statements: dict | None, analyst: dict | None) -> dict:
    """Value/quality/size ratios from S1's raw fundamentals. Any input missing
    -> that ratio is None (skipped downstream, never faked). Quarterly-basis
    ratios (roe, margins, fcf_yield) are consistent across tickers, so they
    rank correctly cross-sectionally even though they aren't annualized."""
    s = statements or {}
    equity = s.get("total_equity")
    ni = s.get("net_income")
    # shares: prefer the value that travels PIT-correctly WITH the statement; fall back
    # to the standalone snapshot. (Fixes market_cap being blank at historical dates.)
    shares = shares or s.get("shares_outstanding")
    market_cap = price * shares if (price and shares) else None
    bvps = _div(equity, shares)
    return {
        "fund.market_cap": market_cap,
        "fund.book_to_price": _div(bvps, price),
        # earnings yield = E/P = net_income / market_cap (== EPS/price, but robust: EPS
        # wasn't reliably available). analyst.trailing_eps was always absent -> None.
        "fund.earnings_yield": _div(ni, market_cap),
        "fund.fcf_yield": _div(s.get("free_cash_flow"), market_cap),
        "fund.roe": _div(s.get("net_income"), equity),
        "fund.gross_profitability": _div(s.get("gross_profit"), s.get("total_assets")),
        "fund.net_margin": _div(s.get("net_income"), s.get("revenue")),
    }


def lagged_returns(closes: list[float], n: int = 7) -> list[float | None]:
    """The n most recent daily returns, newest first. ret[0] = today's realized
    daily return (close[-1]/close[-2]-1). PIT-safe — uses only trailing closes.
    Returns None per lag where history is insufficient."""
    out = []
    for k in range(1, n + 1):
        if len(closes) >= k + 1:
            out.append(closes[-k] / closes[-k - 1] - 1.0)
        else:
            out.append(None)
    return out


def xhorizon_features(closes: list[float]) -> dict[str, float | None]:
    """Long-horizon extension — the champion block from error analysis. Multi-
    month returns + distance from the trailing 252-day high + a new-high flag,
    capturing the 'stretched at a 52-week high after a parabolic run -> reversal-
    down risk' pattern behind the top confident-wrong LONG calls. PIT-safe: uses
    only trailing closes. Each feature is None where history is too short (no
    padding/sentinels — a missing value is genuinely missing)."""
    n = len(closes)
    out = {"xh.ret_21d": None, "xh.ret_63d": None, "xh.ret_126d": None,
           "xh.dist_hi252": None, "xh.new_high_flag": None, "xh.above_hi_streak": None}
    if n < 41:                       # need a few months before extension is meaningful
        return out
    c0 = closes[-1]
    lb = min(252, n)
    hi = max(closes[-lb:])
    if n >= 22:
        out["xh.ret_21d"] = c0 / closes[-22] - 1.0
    if n >= 64:
        out["xh.ret_63d"] = c0 / closes[-64] - 1.0
    if n >= 127:
        out["xh.ret_126d"] = c0 / closes[-127] - 1.0
    out["xh.dist_hi252"] = (c0 - hi) / hi if hi else None
    out["xh.new_high_flag"] = 1.0 if c0 >= 0.98 * hi else 0.0
    # consecutive recent days (<=10) that closed within 3% of the trailing high
    streak = 0
    for k in range(n - 1, max(n - 11, 0), -1):
        lo = max(0, k - lb + 1)
        hk = max(closes[lo:k + 1])
        if hk and closes[k] >= 0.97 * hk:
            streak += 1
        else:
            break
    out["xh.above_hi_streak"] = float(streak)
    return out


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


def _stdev(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def intraday_features(bars_by_day: dict[str, list[tuple]]) -> dict[str, float | None]:
    """Intraday features from HOURLY bars grouped by session date. bars_by_day:
    {YYYY-MM-DD: [(open, close), ...] in time order}. Uses only the sessions present
    (all <= event_date by construction), so it is PIT-safe."""
    days = sorted(bars_by_day)
    if not days:
        return {"tech.intraday_ret": None, "tech.overnight_gap": None, "tech.intraday_vol5": None}
    today = bars_by_day[days[-1]]
    t_open, t_close = today[0][0], today[-1][1]
    intraday_ret = (t_close / t_open - 1) if (t_open and t_close and t_open > 0) else None
    overnight = None
    if len(days) >= 2:
        prev_close = bars_by_day[days[-2]][-1][1]
        if prev_close and t_open and prev_close > 0:
            overnight = t_open / prev_close - 1
    vols = []
    for day in days[-5:]:
        closes = [c for _, c in bars_by_day[day] if c]
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
        v = _stdev(rets)
        if v is not None:
            vols.append(v)
    ivol = (sum(vols) / len(vols)) if vols else None
    return {"tech.intraday_ret": intraday_ret, "tech.overnight_gap": overnight,
            "tech.intraday_vol5": ivol}


_TS = None


def _hourly_by_day(ticker: str, event_date: str, sessions: int = 6) -> dict:
    """Read the HOURLY bars for `ticker` up to and including event_date (PIT), grouped
    by session date. Reads the typed S1 `bars` table (the hourly granularity that the
    daily EOD projection discards)."""
    global _TS
    if _TS is None:
        import schema
        _TS = schema.TypedStore()
    end = event_date + "T99"                                  # include all of event_date
    rows = _TS.c.execute(
        "SELECT bar_ts, open, close FROM bars WHERE ticker=? AND bar_ts<=? "
        "ORDER BY bar_ts DESC LIMIT ?", (ticker, end, sessions * 24)).fetchall()
    by_day: dict[str, list[tuple]] = {}
    for ts, o, c in reversed(rows):                           # back to ascending
        by_day.setdefault(ts[:10], []).append((o, c))
    return by_day


# ═══════════════ Orchestrator ═══════════════

HISTORY_N = 260  # trading days of closes per ticker: covers rsi14/mom20/hvol20
                 # AND the xh.* long-horizon extension block (trailing 252d high).
                 # The short features only read the tail, so the longer read is
                 # a superset — identical results, plus a year of history for xh.*


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
        # collect fundamental ratios to rank cross-sectionally afterward
        fund_by_ticker: dict[str, dict] = {}

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
            price = closes[-1]

            computed = {
                "tech.rsi14": rsi14(closes),
                "tech.mom5": momentum(closes, 5),
                "tech.mom20": momentum(closes, 20),
                "tech.hvol20": hvol20(closes),
                "tech.vr20": volume_ratio20(vols),
            }
            for k, r in enumerate(lagged_returns(closes, 7), start=1):
                computed[f"tech.ret_lag{k}"] = r
            # long-horizon extension (champion block: modeling/ERROR_ANALYSIS.md)
            computed.update(xhorizon_features(closes))
            # INTRADAY granularity from the hourly bars (not the daily EOD projection)
            computed.update(intraday_features(_hourly_by_day(t, event_date)))

            # fundamentals — read S1's raw data as-of event_date (read_asof is
            # publication-date aware, so a statement filed after event_date is
            # invisible here: no lookahead)
            def _val(feature):
                rec = store.read_asof(feature, t, event_date, as_known_at)
                return rec["value"] if rec else None
            computed.update(fundamental_ratios(
                price, _val("fundamental.shares_outstanding"),
                _val("fundamental.statements"), _val("fundamental.analyst_snapshot")))

            for feat, val in computed.items():
                if val is None:
                    skipped[feat] = skipped.get(feat, 0) + 1
                else:
                    rows.append((feat, t, event_date, round(val, 6)))
            if computed["tech.rsi14"] is not None:
                rsi_by_ticker[t] = computed["tech.rsi14"]
            if computed["tech.mom5"] is not None:
                mom5_by_ticker[t] = computed["tech.mom5"]
            fund_by_ticker[t] = computed

        # cross-sectional + regime (need the full universe pass first)
        for t, r in pct_ranks(rsi_by_ticker).items():
            rows.append(("xsec.rank_rsi14", t, event_date, round(r, 4)))
        for t, r in pct_ranks(mom5_by_ticker).items():
            rows.append(("xsec.rank_mom5", t, event_date, round(r, 4)))
        # cross-sectional ranks of the fundamental factors (the selection-
        # relevant form: "cheaper/higher-quality than peers today")
        for feat, rank_name in [("fund.earnings_yield", "xsec.rank_earnings_yield"),
                                ("fund.fcf_yield", "xsec.rank_fcf_yield"),
                                ("fund.roe", "xsec.rank_roe"),
                                ("fund.gross_profitability", "xsec.rank_gross_profitability")]:
            vals = {t: c[feat] for t, c in fund_by_ticker.items() if c.get(feat) is not None}
            for t, r in pct_ranks(vals).items():
                rows.append((rank_name, t, event_date, round(r, 4)))
        if mom5_by_ticker:
            breadth = sum(1 for v in mom5_by_ticker.values() if v > 0) / len(mom5_by_ticker)
            rows.append(("regime.breadth5", MARKET_SCOPE, event_date, round(breadth, 4)))

        store.write_many(rows, trigger_id=trig.trigger_id)
        n_fund = sum(1 for f, *_ in rows if f.startswith("fund.") or "rank_earnings" in f
                     or "rank_fcf" in f or "rank_roe" in f or "rank_gross" in f)
        trig.add_metrics(
            event_date=event_date,
            universe_size=len(universe),
            features_written=len(rows),
            fundamental_features=n_fund,
            skipped=skipped,
        )
        return {"trigger_id": trig.trigger_id, **trig.metrics}


def backfill(universe: list[str], store: FeatureStore | None = None,
             start: str | None = None) -> dict:
    """Produce the FULL S2 feature time series — one pass per trading day we have
    bars for, from `start` (default COLLECTION_START) to the latest. S2 must cover the
    whole range and granularity, not a single date-slice: modeling's panel and the
    dashboards read the complete history. Returns coverage summary."""
    import sqlite3
    from core import DEFAULT_DB
    store = store or FeatureStore()
    start = start or "2025-07-01"
    con = sqlite3.connect(DEFAULT_DB)
    dates = sorted(r[0] for r in con.execute(
        "SELECT DISTINCT event_time FROM feature_values WHERE feature='price.close' "
        "AND event_time >= ?", (start,)))
    for d in dates:
        run_signal_generation(universe, d, store=store)
    return {"dates": len(dates), "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None}
