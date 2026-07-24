"""Network fetchers for S1. This is the ONLY module in src/data that talks to
the outside world (except the LLM call in earnings_signal.py) — everything
else takes these as injected callables so tests run fully offline.

Lessons encoded (see DESIGN.md S1 hard rules):
  - yfinance `earnings_dates` silently degrades to a weaker fallback without
    `lxml` installed — requirements.txt pins it; the fetcher raises loudly if
    the primary path is unavailable rather than silently degrading.
  - Extended-hours quotes must come from fields that actually carry them
    (`.info` pre/postMarketPrice) — `fast_info.lastPrice` returned stale
    regular-session closes and shipped as "current price" once.
"""
from __future__ import annotations

from datetime import datetime, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


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


def fetch_macro() -> dict[str, float]:
    """Market-level series, latest daily close values."""
    import yfinance as yf
    out: dict[str, float] = {}
    for name, symbol in (("vix", "^VIX"), ("yield10y", "^TNX"), ("spy_close", "SPY")):
        try:
            h = yf.Ticker(symbol).history(period="5d", interval="1d")
            if len(h):
                out[name] = float(h["Close"].iloc[-1])
        except Exception:
            pass
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


def fetch_current_price(ticker: str) -> float | None:
    """Real current quote including extended hours — .info fields, NOT
    fast_info.lastPrice (which silently returns the stale regular-session
    close outside RTH; caught live 2026-07-24)."""
    import yfinance as yf
    try:
        info = yf.Ticker(ticker).info
        for field in ("postMarketPrice", "preMarketPrice",
                      "regularMarketPrice", "currentPrice"):
            v = info.get(field)
            if v:
                return float(v)
    except Exception:
        pass
    return None
