# Signal: `bars` — daily OHLCV

| | |
|---|---|
| **Collector kind** | `bars` |
| **Source** | Polygon `/v2/aggs/ticker/{T}/range/1/hour/{from}/{to}` (reliable; yfinance throttled) |
| **Frequency** | `hourly` · full backfill from `COLLECTION_START` (2025-07-01) → now, priority 20 |
| **Typed table** | `bars` (one row per hourly bar, keyed by `bar_ts`) |
| **S1 raw features (projection)** | `price.close`, `price.volume` — **daily EOD** projected to feature_values (last hourly close + summed volume per session) so the daily model still works |
| **Source timestamp column** | `bar_ts` (the hour the bar covers, full timestamp) |

## Downstream consumers (must not break)
- `tech.rsi14`, `tech.mom5`, `tech.mom20`, `tech.hvol20`, `tech.vr20`, `tech.ret_lag1..7` (S2)
- `xh.new_high_flag`, `xh.above_hi_streak` (S2)
- `fund.market_cap` and every `fund.*` ratio's price term (S2)
- All S3 predictors (price/return features), S4 alpha via `predict.eod_return`

## Schema (`bars`)
```sql
CREATE TABLE bars (
  ticker TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  bar_ts TEXT,                         -- SOURCE timestamp: the hour (full ISO datetime)
  ingested_at TEXT NOT NULL,           -- when WE collected it
  PRIMARY KEY (ticker, bar_ts)
);
```

## Example collected raw data (real row)
```json
{"ticker":"AAPL","open":257.7,"high":258.1,"low":257.5,"close":257.74,
 "volume":812334.0,"bar_ts":"2025-10-06T09:00:00+00:00","ingested_at":"2026-07-26T…Z"}
```
Daily consumers read the **EOD projection** (`price.close`/`price.volume` per session =
last hourly close + summed volume), so intraday granularity is stored without breaking
the daily EOD model.

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
Expected depth ≈ trading hours since `COLLECTION_START` (~7 × trading days). Missing =
expected hourly points over `[2025-07-01, now]` minus distinct `bar_ts` per ticker
(see README §2.2). Backfill sweeps the universe via Polygon (5/min).
**Note:** the daily EOD projection (`price.close`) is what the S2 tech features read; the
SQL examples above operate on that daily view — replace `date` with the projected daily
series or aggregate `bar_ts` to the session for equivalent logic.
**Live gap:** typed `bars` currently holds only AAPL — universe backfill pending
daemon kickstart.
