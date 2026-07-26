# Signal: `short_interest` — FINRA bi-monthly short interest

| | |
|---|---|
| **Collector kind** | `short` |
| **Source** | yfinance `Ticker.info` (`sharesShort`, `shortPercentOfFloat`, `shortRatio`, `sharesShortPriorMonth`, `dateShortInterest`) |
| **Frequency** | `snapshot` · 3-day poll (priority 42) · yfinance gives current print only |
| **Typed table** | `short_interest` |
| **S1 raw features** | `short.shares`, `short.pct_float`, `short.days_to_cover`, `short.change_pct` |
| **Source timestamp column** | `settlement_date` (the FINRA settlement date the print refers to) |

## Downstream consumers
- Near-earnings crowding / squeeze-fuel module (short interest was shown to *hurt* the
  daily directional model, so it feeds the event module, not the daily model).

## Schema (`short_interest`)
```sql
CREATE TABLE short_interest (
  ticker TEXT, shares_short REAL, pct_float REAL, days_to_cover REAL, change_pct REAL,
  settlement_date TEXT,        -- SOURCE timestamp: FINRA settlement date
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, settlement_date)
);
```

## Example collected raw data (real row)
```json
{"ticker":"AAPL","shares_short":146547784.0,"pct_float":0.01,"days_to_cover":2.28,
 "change_pct":0.0159,"settlement_date":"2026-07-15","ingested_at":"2026-07-26T00:07:05.85Z"}
```

## After processing
Consumed near-raw as crowding features; `change_pct = (shares_short - prior)/prior`.
```sql
-- latest crowding snapshot with squeeze-fuel and buildup direction
SELECT pct_float, days_to_cover,
       CASE WHEN change_pct > 0 THEN 'buildup' ELSE 'covering' END AS short_trend
FROM short_interest WHERE ticker=:T ORDER BY settlement_date DESC LIMIT 1;
```

## Coverage expectation & missing-data check — an honest limitation
**yfinance returns only the current + prior print.** A full dissemination-dated history
requires FINRA's bi-monthly archive files (see `docs/DATA_SOURCES.md`, paid/free options).
So snapshot mode is correct: coverage = tickers with a current print; real history accrues
forward. A strict as-of backtest must lag the `settlement_date` by ~8 business days (FINRA
dissemination gap).
