# Signal: `analyst_snapshot` — daily consensus snapshot

| | |
|---|---|
| **Collector kind** | `analyst` |
| **Source** | yfinance `Ticker.info` (`forwardEps`, `numberOfAnalystOpinions`, `recommendationMean`, `targetMeanPrice`) |
| **Frequency** | `snapshot` · daily poll (priority 40) |
| **Typed table** | *(none yet — stored as JSON in `feature_values`)* |
| **S1 raw feature** | `fundamental.analyst_snapshot` |
| **Source timestamp column** | ingestion day (current-state snapshot) |

## Downstream consumers
- Consensus level (target price, recommendation mean) features.
- A **revision time series** accrues going forward (yfinance can't backfill consensus).

## Schema
> **Schema debt:** this signal currently writes a JSON blob to `feature_values`, not a
> typed table. It **should** get a typed table for consistency with the rest of S1.
> Proposed:
```sql
CREATE TABLE analyst_snapshot (
  ticker TEXT, forward_eps REAL, n_analysts INT,
  recommendation_mean REAL, target_mean_price REAL,
  snap_date TEXT, ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, snap_date)
);
```

## Example collected raw data (current JSON shape)
```json
{"forward_eps":7.42,"n_analysts":41,"recommendation_mean":2.0,"target_mean_price":355.0}
```

## After processing
```sql
-- consensus upside vs live price; daily deltas form the revision series
SELECT target_mean_price / :live_price - 1.0 AS consensus_upside,
       recommendation_mean, n_analysts;
```

## Coverage expectation & missing-data check
Snapshot mode: coverage = tickers with a current snapshot; revision history accrues
forward only. Distinct from `analyst_revisions` (rolling event list of up/downgrades).
