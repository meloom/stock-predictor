"""s1_data.py — Stage S1: data ingestion (DESIGN.md §S1), one file.

Sections: feature registry (the ingestion contract) · network fetchers (the
only outbound I/O) · grounded LLM earnings extraction · the daily orchestrator.
Project style: one file per stage; simple, working. Tests: tests/test_s1_data.py.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

from core import Trigger, FeatureStore, MARKET_SCOPE

ET = ZoneInfo("America/New_York")

# ═══════════════ Feature registry — the ingestion contract ═══════════════
S1_FEATURES = [
    # name, dtype, scope_kind, cadence, point-in-time rule
    ("price.close",  "float", "ticker", "daily",
     "Known after the trading session close it describes."),
    ("price.volume", "float", "ticker", "daily",
     "Known after the trading session close it describes."),
    ("price.current", "json", "ticker", "intraday",
     "Live current price WITH trading session {price, session: pre|regular|"
     "post|closed}. THE price for display/sizing/decisions — never price.close "
     "(the prior bar). Session-aware: reflects pre- and post-market."),
    ("macro.vix",    "float", "market", "daily",
     "Known after the session close it describes."),
    ("macro.yield10y", "float", "market", "daily",
     "Known after the session close it describes."),
    ("macro.spy_close", "float", "market", "daily",
     "Known after the session close it describes."),
    ("calendar.days_to_earnings", "int", "ticker", "daily",
     "Computed from the published earnings calendar as of the ingestion "
     "moment; forward-looking by nature, revisable by the issuer."),
    ("fundamental.earnings_signal", "json", "ticker", "event",
     "Extracted from the most recent PUBLISHED earnings report via grounded "
     "search; event_time = report date; only valid after publication."),
    ("fundamental.shares_outstanding", "float", "ticker", "daily",
     "Shares outstanding snapshot — needed for market cap and any per-share "
     "ratio. event_time = ingestion day (current-state snapshot)."),
    ("fundamental.statements", "json", "ticker", "event",
     "Latest quarterly income/balance/cashflow line items. CRITICAL: "
     "event_time = the actual earnings ANNOUNCEMENT date (not fiscal period "
     "end), falling back to period_end + REPORTING_LAG_DAYS. This is the "
     "publication-date discipline that prevents fundamentals lookahead — a "
     "statement is only 'known' once filed, weeks after the quarter closes."),
    ("fundamental.analyst_snapshot", "json", "ticker", "daily",
     "Daily snapshot of consensus {forward_eps, n_analysts, "
     "recommendation_mean, target_mean_price}. Snapshotted daily so a REVISION "
     "time series accrues going forward — analyst revisions cannot be "
     "backfilled from yfinance, they must be collected from now on."),
    # -- SHORT INTEREST (crowding / squeeze-fuel; feeds the future near-earnings
    #    module, NOT the daily directional model which it was shown to hurt). The
    #    FINRA bi-monthly print. event_time = the SETTLEMENT date the print refers
    #    to; FINRA disseminates it ~8 business days LATER, so a strict as-of
    #    backtest must lag by that gap — a full dissemination-dated history needs
    #    FINRA's archive files (see docs/DATA_SOURCES.md). yfinance gives only the
    #    current + prior print, so daily ingestion accrues real history going
    #    forward (same pattern as analyst_snapshot). --
    ("short.shares", "float", "ticker", "biweekly",
     "Shares sold short (latest FINRA bi-monthly print)."),
    ("short.pct_float", "float", "ticker", "biweekly",
     "Short interest as a fraction of float — the crowding signal."),
    ("short.days_to_cover", "float", "ticker", "biweekly",
     "Short interest / avg daily volume — squeeze fuel (short ratio)."),
    ("short.change_pct", "float", "ticker", "biweekly",
     "(shares_short - prior print) / prior — short buildup (+) vs covering (-)."),
]

# Conservative reporting lag: a quarterly statement is treated as knowable this
# many days after the fiscal period end, UNLESS we have the real announcement
# date (which we prefer). 60d safely covers the 10-Q filing window (~40-45d for
# large caps). Erring late never creates lookahead; erring early would.
REPORTING_LAG_DAYS = 60


def register_all(store: FeatureStore) -> None:
    for name, dtype, scope_kind, cadence, pit_rule in S1_FEATURES:
        store.register(name, dtype, scope_kind, source_stage="S1",
                       cadence=cadence, pit_rule=pit_rule)

# ═══════════════ Network fetchers — the only outbound I/O ═══════════════
# Lessons encoded: lxml required or earnings_dates silently degrades;
# extended-hours quotes need .info pre/postMarketPrice (fast_info.lastPrice
# returned stale closes, shipped once); None over sentinels (999 leaked as a
# plausible-looking feature value in the predecessor).

def fetch_daily_bars(tickers: list[str], period: str = "10d") -> dict[str, list[dict]]:
    """{ticker: [{date, close, volume}, ...]} — most recent daily bars."""
    import yfinance as yf
    out: dict[str, list[dict]] = {}
    data = yf.download(tickers, period=period, interval="1d",
                       group_by="ticker", progress=False, threads=True)
    for t in tickers:
        try:
            h = data[t].dropna() if len(tickers) > 1 else data.dropna()
            out[t] = [
                {"date": idx.date().isoformat(),
                 "close": float(row["Close"]),
                 "volume": float(row["Volume"])}
                for idx, row in h.iterrows()
            ]
        except Exception:
            out[t] = []
    return out


def fetch_macro(period: str = "90d") -> dict[str, list[dict]]:
    """Market-level series WITH history: {name: [{date, value}, ...]}.
    History matters: S3's regime gate needs 20 days of SPY closes — the
    original latest-value-only fetch left the gate permanently unable to
    compute its trend component (caught by the first real S3 trigger
    failing toward cash with 'spy_close(1/20 days)')."""
    import yfinance as yf
    out: dict[str, list[dict]] = {}
    for name, symbol in (("vix", "^VIX"), ("yield10y", "^TNX"), ("spy_close", "SPY")):
        try:
            h = yf.Ticker(symbol).history(period=period, interval="1d")
            out[name] = [{"date": idx.date().isoformat(), "value": float(row["Close"])}
                         for idx, row in h.iterrows()]
        except Exception:
            out[name] = []
    return out


def fetch_days_to_earnings(ticker: str, asof: date | None = None) -> int | None:
    """Days until next earnings via the primary earnings_dates path, calendar
    fallback second. Returns None (not a sentinel) when genuinely unknown."""
    import yfinance as yf
    asof = asof or datetime.now(ET).date()
    tk = yf.Ticker(ticker)
    dates: list[date] = []
    try:
        cal = tk.earnings_dates
        if cal is not None and len(cal):
            dates = sorted(d.date() if hasattr(d, "date") else d for d in cal.index)
    except Exception:
        pass
    if not dates:
        try:
            cal = tk.calendar
            if isinstance(cal, dict) and cal.get("Earnings Date"):
                raw = cal["Earnings Date"]
                dates = sorted(d if isinstance(d, date) else d.date()
                               for d in (raw if isinstance(raw, list) else [raw]))
        except Exception:
            pass
    for d in dates:
        delta = (d - asof).days
        if delta >= 0:
            return delta
    return None


def fetch_current_quote(ticker: str) -> dict | None:
    """Real CURRENT price WITH its trading session — the price the user has
    repeatedly (and rightly) insisted on: pre-market / regular / post-market
    aware, NEVER the stale prior close.

    Picks the field by marketState, NOT fast_info.lastPrice (which returns the
    stale regular-session close outside RTH — the INTC $100.23-vs-$103 bug).
    Returns {"price": float, "session": "pre"|"regular"|"post"|"closed"} or None.
    """
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        return None
    state = (info.get("marketState") or "").upper()
    pre, post = info.get("preMarketPrice"), info.get("postMarketPrice")
    reg = info.get("regularMarketPrice") or info.get("currentPrice")
    if state.startswith("PRE") and pre:
        return {"price": float(pre), "session": "pre"}
    if state.startswith("POST") and post:
        return {"price": float(post), "session": "post"}
    if reg:
        return {"price": float(reg), "session": "regular" if state == "REGULAR" else "closed"}
    # last resort: any extended-hours quote beats nothing (still never a stale bar)
    if post:
        return {"price": float(post), "session": "post"}
    if pre:
        return {"price": float(pre), "session": "pre"}
    return None

# ═══════════════ Fundamentals: raw statement data for S2 ═══════════════
# S2's value/quality/growth features are price ÷ statement-item. S2 can only
# DERIVE — S1 must collect the raw items first. The one thing that makes this
# valid rather than lookahead-riddled is the publication date (below).


def fetch_shares_outstanding(ticker: str) -> float | None:
    import yfinance as yf
    try:
        v = yf.Ticker(ticker).info.get("sharesOutstanding")
        return float(v) if v else None
    except Exception:
        return None


def _last_earnings_announcement(tk, asof: date) -> date | None:
    """The most recent PAST earnings announcement date — the real publication
    date of the latest statements. Far better than a lag guess when available."""
    try:
        cal = tk.earnings_dates
        if cal is not None and len(cal):
            past = sorted(d.date() if hasattr(d, "date") else d
                          for d in cal.index
                          if (d.date() if hasattr(d, "date") else d) <= asof)
            return past[-1] if past else None
    except Exception:
        pass
    return None


def _row(df, *labels) -> float | None:
    """First matching row's most-recent-column value from a yfinance statement
    DataFrame (row labels vary across versions, so try several)."""
    if df is None or len(df.columns) == 0:
        return None
    for label in labels:
        if label in df.index:
            try:
                v = df.loc[label].iloc[0]  # columns are newest-first
                return float(v) if v == v else None  # NaN -> None
            except Exception:
                continue
    return None


def fetch_latest_statements(ticker: str, asof: date | None = None) -> dict | None:
    """Latest quarterly income/balance/cashflow line items, stamped with the
    real earnings-announcement date (fallback: period_end + REPORTING_LAG_DAYS).
    Returns None if nothing usable. Missing individual line items are None,
    never fabricated."""
    import yfinance as yf
    asof = asof or datetime.now(ET).date()
    try:
        tk = yf.Ticker(ticker)
        inc, bal, cf = tk.quarterly_income_stmt, tk.quarterly_balance_sheet, tk.quarterly_cashflow
    except Exception:
        return None
    if inc is None or len(getattr(inc, "columns", [])) == 0:
        return None
    period_end = inc.columns[0]
    period_end = period_end.date() if hasattr(period_end, "date") else period_end

    announced = _last_earnings_announcement(tk, asof)
    from datetime import timedelta
    event_time = (announced or (period_end + timedelta(days=REPORTING_LAG_DAYS)))

    return {
        "period_end": period_end.isoformat(),
        "event_time": event_time.isoformat(),
        "publish_source": "announcement" if announced else f"period_end+{REPORTING_LAG_DAYS}d",
        "revenue": _row(inc, "Total Revenue", "Operating Revenue"),
        "gross_profit": _row(inc, "Gross Profit"),
        "operating_income": _row(inc, "Operating Income", "Total Operating Income As Reported"),
        "net_income": _row(inc, "Net Income", "Net Income Common Stockholders"),
        "ebitda": _row(inc, "EBITDA", "Normalized EBITDA"),
        "total_assets": _row(bal, "Total Assets"),
        "total_equity": _row(bal, "Stockholders Equity", "Total Equity Gross Minority Interest"),
        "total_debt": _row(bal, "Total Debt"),
        "cash": _row(bal, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"),
        "operating_cash_flow": _row(cf, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
        "capex": _row(cf, "Capital Expenditure"),
        "free_cash_flow": _row(cf, "Free Cash Flow"),
    }


def fetch_analyst_snapshot(ticker: str) -> dict | None:
    """Today's consensus snapshot. Snapshotted DAILY so a revision time series
    accrues going forward (yfinance can't backfill revision history)."""
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        return None
    snap = {
        "forward_eps": info.get("forwardEps"),
        "trailing_eps": info.get("trailingEps"),
        "n_analysts": info.get("numberOfAnalystOpinions"),
        "recommendation_mean": info.get("recommendationMean"),
        "target_mean_price": info.get("targetMeanPrice"),
    }
    return snap if any(v is not None for v in snap.values()) else None


def fetch_short_interest(ticker: str) -> dict | None:
    """Short-interest snapshot from the latest FINRA bi-monthly print (via yfinance
    .info). Returns event_time = the SETTLEMENT date the print refers to
    (dateShortInterest), so it lands PIT-correctly in the store, plus the level and
    the change vs the prior print (buildup/covering). Returns None if the field is
    absent. NOTE: FINRA disseminates ~8 business days after settlement — see
    docs/DATA_SOURCES.md for the dissemination-lag caveat and the archive path to a
    full historical series."""
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info
    except Exception:
        return None
    shares = info.get("sharesShort")
    if shares is None:
        return None
    prior = info.get("sharesShortPriorMonth")
    ts = info.get("dateShortInterest")
    event_time = None
    if ts:
        try:
            event_time = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        except Exception:
            event_time = None
    change_pct = ((float(shares) - float(prior)) / float(prior)
                  if prior not in (None, 0) else None)
    return {
        "event_time": event_time,
        "shares_short": float(shares),
        "pct_float": info.get("shortPercentOfFloat"),
        "days_to_cover": info.get("shortRatio"),
        "change_pct": change_pct,
    }

# ═══════════════ Grounded LLM earnings extraction ═══════════════

SIGNAL_MODEL = "claude-sonnet-4-5"
RECENT_REPORT_WINDOW_DAYS = 5

SYSTEM_PROMPT = """You are an equity-research analyst extracting structured signals \
from a company's MOST RECENT quarterly earnings report, for a systematic trading \
system. Ground every factual claim in an actual web search this call — never assert \
a number or direction you have not just found. If you cannot find evidence of an \
earnings report within the lookback window, say so explicitly rather than guessing.

Extract these fields, in priority order (from a 65-report study of what actually \
explains next-day price reaction):

1. guidance_direction — the single strongest signal: raised, maintained, lowered, \
or withdrawn, with specific before/after numbers when found.
2. capex_trend and capex_framing — second strongest; can override guidance. Is capex \
accelerating/stable/decelerating, and is management framing it as monetizable growth \
(backlog, committed demand) or does it read as margin/FCF erosion? The same capex \
increase has produced opposite reactions depending on framing alone.
3. adj_eps_surprise_pct and revenue_surprise_pct — ADJUSTED (non-GAAP) vs consensus, \
NOT GAAP. Note any one-time items and the size of the GAAP/adjusted gap.
4. one_time_items — what drives any GAAP/adjusted divergence.

Respond with ONLY a JSON object (no markdown):
{
  "has_recent_report": true|false,
  "report_date": "YYYY-MM-DD" or null,
  "days_since_report": <int> or null,
  "guidance_direction": "raised"|"maintained"|"lowered"|"withdrawn"|"unknown",
  "guidance_detail": "<short>",
  "capex_trend": "accelerating"|"stable"|"decelerating"|"unknown",
  "capex_framing": "growth_positive"|"margin_concern"|"not_material"|"unknown",
  "adj_eps_surprise_pct": <float or null>,
  "revenue_surprise_pct": <float or null>,
  "one_time_items": [<short strings>],
  "net_signal": "bullish"|"bearish"|"neutral"|"insufficient_data",
  "confidence": "HIGH"|"MEDIUM"|"LOW",
  "reasoning": "<2-3 sentences>"
}
"""


def extract_earnings_signal(ticker: str, trigger: Trigger,
                            asof_date=None, client=None) -> dict:
    """One grounded extraction call. Cost lands on `trigger`. Fails closed."""
    import anthropic
    asof_date = asof_date or datetime.now(ET).date()
    asof_str = asof_date.isoformat() if hasattr(asof_date, "isoformat") else str(asof_date)
    client = client or anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    user_prompt = (
        f"Ticker: {ticker}\nAs-of date: {asof_str}\n\n"
        f"Search for whether {ticker} reported quarterly earnings within the last "
        f"{RECENT_REPORT_WINDOW_DAYS} trading days of {asof_str}. If yes, extract the "
        f"signals above from that report. If no recent report, set "
        f"has_recent_report=false — do not describe an older report as recent.")

    try:
        msg = client.messages.create(
            model=SIGNAL_MODEL,
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            messages=[{"role": "user", "content": user_prompt}],
        )
        usage = getattr(msg, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        server_tools = getattr(usage, "server_tool_use", None)
        searches = getattr(server_tools, "web_search_requests", 0) or 0
        trigger.record_cost(provider="claude-sonnet", tokens_in=tokens_in,
                            tokens_out=tokens_out, web_searches=searches,
                            note=f"earnings_signal:{ticker}")

        text = "".join(b.text for b in msg.content if b.type == "text")
        start, end = text.find("{"), text.rfind("}")
        result = json.loads(text[start:end + 1])
        result.setdefault("one_time_items", [])
        result["ticker"] = ticker
        return result
    except Exception as e:
        return {"ticker": ticker, "has_recent_report": False,
                "net_signal": "insufficient_data", "confidence": "LOW",
                "error": str(e)}

# ═══════════════ Daily ingestion orchestrator ═══════════════

def run_daily_ingestion(universe: list[str],
                        store: FeatureStore | None = None,
                        fetch_bars=fetch_daily_bars,
                        fetch_macro=fetch_macro,
                        fetch_dte=fetch_days_to_earnings,
                        fetch_quote=fetch_current_quote,
                        fetch_shares=fetch_shares_outstanding,
                        fetch_statements=fetch_latest_statements,
                        fetch_analyst=fetch_analyst_snapshot,
                        fetch_short=fetch_short_interest,
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

        # -- LIVE current price, session-aware (pre/regular/post) ------------
        # THE price for display/sizing/decisions — never price.close. Wired
        # here so it can't be "defined but forgotten" again.
        current_ok, current_by_session = 0, {}
        for t in universe:
            q = fetch_quote(t) if fetch_quote else None
            if q is None:
                continue
            store.write("price.current", t, today, q, trigger_id=trig.trigger_id)
            current_ok += 1
            current_by_session[q["session"]] = current_by_session.get(q["session"], 0) + 1

        # -- macro (with history — S3's regime gate needs 20d of SPY) --------
        macro = fetch_macro()
        macro_rows = [(f"macro.{k}", MARKET_SCOPE, pt["date"], pt["value"])
                      for k, series in macro.items() for pt in series]
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

        # -- fundamentals: raw statement data for S2's value/quality features -
        # Publication-date discipline lives in fetch_latest_statements: a
        # statement's event_time is the real announcement date (or a
        # conservative lag), so backtests never see it before it was filed.
        shares_ok, stmts_ok, analyst_ok, short_ok = 0, 0, 0, 0
        for t in universe:
            sh = fetch_shares(t) if fetch_shares else None
            if sh is not None:
                store.write("fundamental.shares_outstanding", t, today, sh,
                            trigger_id=trig.trigger_id)
                shares_ok += 1
            st = fetch_statements(t) if fetch_statements else None
            if st is not None:
                # event_time = publication date, NOT the fiscal period end
                store.write("fundamental.statements", t, st["event_time"], st,
                            trigger_id=trig.trigger_id)
                stmts_ok += 1
            an = fetch_analyst(t) if fetch_analyst else None
            if an is not None:
                store.write("fundamental.analyst_snapshot", t, today, an,
                            trigger_id=trig.trigger_id)
                analyst_ok += 1
            # short interest — event_time = the print's settlement date (PIT), so
            # it's placed on the day the position was measured, not ingestion day.
            si = fetch_short(t) if fetch_short else None
            if si is not None:
                et = si["event_time"] or today
                for feat, key in (("short.shares", "shares_short"),
                                  ("short.pct_float", "pct_float"),
                                  ("short.days_to_cover", "days_to_cover"),
                                  ("short.change_pct", "change_pct")):
                    if si[key] is not None:
                        store.write(feat, t, et, si[key], trigger_id=trig.trigger_id)
                short_ok += 1

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
            current_prices_ok=current_ok,
            current_price_sessions=current_by_session,
            shares_ok=shares_ok,
            statements_ok=stmts_ok,
            analyst_snapshots_ok=analyst_ok,
            short_interest_ok=short_ok,
            macro_series=sorted(k for k, s in macro.items() if s),
            calendar_ok=cal_ok,
            calendar_unknown=cal_unknown,
            earnings_checked=len(check_list),
            earnings_signals_written=signals_written,
        )
        return {"trigger_id": trig.trigger_id, **trig.metrics}
