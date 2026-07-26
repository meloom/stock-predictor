# Signal: `fundamentals` — quarterly statements + shares outstanding

| | |
|---|---|
| **Collector kind** | `statements` (also writes `fundamental.shares_outstanding`) |
| **Source** | yfinance quarterly income / balance / cashflow + `sharesOutstanding` |
| **Frequency** | `event` · daily poll (priority 50) · collects all reported quarters |
| **Typed table** | `fundamentals` |
| **S1 raw features** | `fundamental.statements`, `fundamental.shares_outstanding` |
| **Source timestamp column** | `publish_date` (announcement date, NOT fiscal period end) |

## Downstream consumers (the whole value factor set)
- `fund.market_cap` = live price × `shares_outstanding`
- `fund.earnings_yield`, `fund.book_to_price`, `fund.fcf_yield`, `fund.roe`,
  `fund.net_margin`, `fund.gross_profitability` (S2)
- `xsec.rank_earnings_yield`, `xsec.rank_fcf_yield`, `xsec.rank_gross_profitability`,
  `xsec.rank_roe` (S2 cross-sectional ranks) → S3 predictors.

## Point-in-time discipline
`publish_date` = the actual earnings **announcement** date, falling back to
`period_end + REPORTING_LAG_DAYS (60)`. A statement is only "known" once filed (weeks
after quarter close) — this prevents fundamentals lookahead. `read_asof` enforces it.

## Schema (`fundamentals`)
```sql
CREATE TABLE fundamentals (
  ticker TEXT, period_end TEXT, revenue REAL, net_income REAL, total_equity REAL,
  gross_profit REAL, total_assets REAL, free_cash_flow REAL, trailing_eps REAL,
  shares_outstanding REAL,
  publish_date TEXT,           -- SOURCE timestamp: announcement / knowable date
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, publish_date)
);
```

## Example collected raw data (real row)
```json
{"ticker":"AAPL","period_end":"2026-03-31","revenue":111184000000.0,
 "net_income":29578000000.0,"total_equity":106491000000.0,"gross_profit":54781000000.0,
 "total_assets":371082000000.0,"free_cash_flow":26731000000.0,"trailing_eps":null,
 "shares_outstanding":14687356000.0,"publish_date":"2026-04-30","ingested_at":"…"}
```

## After processing (S2 value factors + cross-sectional ranks)
```sql
-- per-name value ratios (market_cap uses the LIVE price)
WITH f AS (SELECT * FROM fundamentals WHERE ticker=:T ORDER BY publish_date DESC LIMIT 1)
SELECT
  :live_price * f.shares_outstanding                     AS market_cap,
  f.net_income / NULLIF(:live_price*f.shares_outstanding,0) AS earnings_yield,
  f.total_equity / NULLIF(:live_price*f.shares_outstanding,0) AS book_to_price,
  f.free_cash_flow / NULLIF(:live_price*f.shares_outstanding,0) AS fcf_yield,
  f.net_income / NULLIF(f.total_equity,0)                AS roe,
  f.net_income / NULLIF(f.revenue,0)                     AS net_margin,
  f.gross_profit / NULLIF(f.total_assets,0)              AS gross_profitability
FROM f;

-- xsec.rank_* : percentile rank of each ratio across the universe (S2 pct_ranks)
SELECT ticker, PERCENT_RANK() OVER (ORDER BY earnings_yield) AS rank_earnings_yield FROM ...;
```

## Coverage expectation & missing-data check
Rolling mode: yfinance returns the last several quarters. Coverage = tickers with ≥1
statement + span. **Live gap:** typed table holds 1 row (AAPL) — universe backfill
pending kickstart.
