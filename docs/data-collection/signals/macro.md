# Signal: `macro` — market-wide daily series

| | |
|---|---|
| **Collector kind** | `macro` (scope = `_market`, not per-ticker) |
| **Source** | yfinance history for `^VIX`, `^TNX`, `SPY` (90d) |
| **Mode / cadence** | `history` · daily (interval 1d, priority 10) · `backfill_days=90` |
| **Typed table** | `macro` |
| **S1 raw features** | `macro.vix`, `macro.yield10y`, `macro.spy_close` |
| **Source timestamp column** | `date` |

## Downstream consumers
- `alpha.regime` — VIX/SPY regime gate (S4)
- `alpha.event_risk` — market-stress component (S4)
- Market context in S3 predictors and S4 alpha

## Schema (`macro`)
```sql
CREATE TABLE macro (
  name TEXT,           -- 'vix' | 'yield10y' | 'spy_close'
  value REAL,
  date  TEXT,          -- SOURCE timestamp: session date
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (name, date)
);
```

## Example collected raw data (real row)
```json
{"name":"spy_close","value":null,"date":"2026-07-24","ingested_at":"2026-07-26T00:07:05.81Z"}
```
> **Live bug flagged by coverage:** `spy_close` is storing `value=null`. `vix`/`yield10y`
> populate; `_h_macro` must be fixed to write the SPY close. Tracked in README §2.3.

## After processing (S4)
```sql
-- regime inputs: latest VIX level and 20d SPY trend
SELECT
  (SELECT value FROM macro WHERE name='vix'       ORDER BY date DESC LIMIT 1) AS vix,
  (SELECT value FROM macro WHERE name='spy_close'  ORDER BY date DESC LIMIT 1) /
  (SELECT value FROM macro WHERE name='spy_close'  ORDER BY date DESC LIMIT 1 OFFSET 20) - 1.0
      AS spy_trend_20d;
```

## Coverage expectation & missing-data check
History mode: expected ≈ trading days in 90d per series. Missing = calendar days minus
distinct `date` per `name`. Currently 3 series present, 754 rows (~251 days each).
