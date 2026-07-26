# Signal: `bars` — daily OHLCV

| | |
|---|---|
| **Collector kind** | `bars` |
| **Source** | Polygon `/v2/aggs/ticker/{T}/range/1/day/{from}/{to}` (reliable; yfinance throttled) |
| **Mode / cadence** | `history` · daily (interval 1d, priority 20) · `backfill_days=90` |
| **Typed table** | `bars` |
| **S1 raw features (projection)** | `price.close`, `price.volume` |
| **Source timestamp column** | `date` (the trading session the bar closed) |

## Downstream consumers (must not break)
- `tech.rsi14`, `tech.mom5`, `tech.mom20`, `tech.hvol20`, `tech.vr20`, `tech.ret_lag1..7` (S2)
- `xh.new_high_flag`, `xh.above_hi_streak` (S2)
- `fund.market_cap` and every `fund.*` ratio's price term (S2)
- All S3 predictors (price/return features), S4 alpha via `predict.eod_return`

## Schema (`bars`)
```sql
CREATE TABLE bars (
  ticker TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  date   TEXT,                         -- SOURCE timestamp: session date (YYYY-MM-DD)
  ingested_at TEXT NOT NULL,           -- when WE collected it
  PRIMARY KEY (ticker, date)
);
```

## Example collected raw data (real row)
```json
{"ticker":"AAPL","open":321.79,"high":334.37,"low":321.62,"close":333.02,
 "volume":47489415.9,"date":"2026-07-24","ingested_at":"2026-07-26T00:07:04.82Z"}
```

## After processing (S2 features derived from this raw)
```sql
-- tech.mom20 : 20-trading-day price momentum
WITH px AS (SELECT date, close FROM bars WHERE ticker=:T ORDER BY date DESC LIMIT 21)
SELECT (SELECT close FROM px ORDER BY date DESC LIMIT 1) /
       (SELECT close FROM px ORDER BY date ASC  LIMIT 1) - 1.0 AS mom20;

-- tech.ret_lag1..7 : trailing daily simple returns (input to RSI/hvol/vr)
SELECT date, close/LAG(close) OVER (ORDER BY date) - 1.0 AS ret
FROM bars WHERE ticker=:T ORDER BY date DESC LIMIT 8;

-- xh.new_high_flag : is today the max close of the trailing window?
SELECT (SELECT close FROM bars WHERE ticker=:T ORDER BY date DESC LIMIT 1)
        >= MAX(close) AS new_high_flag
FROM bars WHERE ticker=:T AND date >= date('now','-60 day');
```
(Implemented in `src/s2_signals.py`: `rsi14`, `momentum`, `hvol20`, `volume_ratio20`,
`lagged_returns`, `xhorizon_features`. SQL above is the equivalent logic.)

## Coverage expectation & missing-data check
Expected depth ≈ trading days in `backfill_days` (~64 for 90d). Missing = trading-day
calendar over `[start, today]` minus distinct `date` per ticker (see README §2.2).
**Live gap:** typed `bars` currently holds only AAPL — universe backfill pending
daemon kickstart.
