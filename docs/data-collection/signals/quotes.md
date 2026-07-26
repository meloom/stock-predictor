# Signal: `quotes` — live current price (session-aware)

| | |
|---|---|
| **Collector kind** | `quote` |
| **Source** | yfinance `Ticker.info` (pre / regular / post market aware) |
| **Mode / cadence** | `snapshot` · every 6h (interval 21600s, priority 30) |
| **Typed table** | `quotes` |
| **S1 raw feature (projection)** | `price.current` |
| **Source timestamp column** | `quote_ts` (the market timestamp the price was logged at source) |

## Downstream consumers
- Live pricing / execution reference price (never prior close — see collaboration rule).
- Intraday reference for value-based position sizing.

## Schema (`quotes`)
```sql
CREATE TABLE quotes (
  ticker TEXT, price REAL, session TEXT,     -- session ∈ pre|regular|post|closed
  quote_ts TEXT,                             -- SOURCE timestamp (exact market time)
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, quote_ts)
);
```

## Example collected raw data (real row)
```json
{"ticker":"AAPL","price":333.02,"session":"closed",
 "quote_ts":"2026-07-24T20:00:01+00:00","ingested_at":"2026-07-26T00:07:05.58Z"}
```
`quote_ts` is the **exact source time** the price was logged — from `preMarketTime` /
`regularMarketTime` / `postMarketTime` depending on session — NOT the collection date.

## After processing
No S2 feature is *derived* from quotes; it is consumed directly as the live price. Each
6h snapshot appends one row, so an intraday reference-price series accrues forward:
```sql
-- most recent live price and its exact source timestamp
SELECT price, session, quote_ts FROM quotes WHERE ticker=:T ORDER BY quote_ts DESC LIMIT 1;
```

## Coverage expectation & missing-data check
Snapshot mode: coverage = tickers with a current row; history accrues forward (cannot be
backfilled). Missing = tickers with no row, or newest `quote_ts` stale > interval.
