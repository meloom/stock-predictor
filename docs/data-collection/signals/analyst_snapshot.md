# Signal: `analyst_snapshot` — daily consensus snapshot

| | |
|---|---|
| **Collector kind** | `analyst` |
| **Source** | yfinance `Ticker.info` (eps/target) **+ `Ticker.recommendations`** (4-month consensus history) |
| **Frequency** | `snapshot` · daily poll (priority 40) |
| **Typed table** | *(none yet — stored as JSON in `feature_values`)* |
| **S1 raw feature** | `fundamental.analyst_snapshot` |
| **Source timestamp column** | `event_time` — today for the live point, **back-dated ~30·k days** for each earlier month |

## EARLIER signal (not just today) — implemented
The daily `Ticker.info` snapshot is current-state only. To get *earlier* consensus we
also read yfinance **`Ticker.recommendations`**, which returns the analyst distribution
(strongBuy/buy/hold/sell/strongSell) for the last **4 monthly buckets** (`0m,-1m,-2m,-3m`).
We reconstruct a `recommendation_mean` (1=strongBuy … 5=strongSell) from each bucket and
**back-date** it, so ~3 months of prior consensus land immediately instead of only
accruing forward (`s1_data.fetch_analyst_history`). The whole `recommendation_mean` series
uses this ONE reconstructed method (not `info.recommendationMean`, which is a different
metric) so there is no artificial jump between back-dated and live points.

Deeper history is available from **`Ticker.upgrades_downgrades`** (years of dated
rating + price-target changes) — collected separately as [`analyst_revisions`](analyst_revisions.md).

## Downstream consumers
- Consensus level + **trend** (target price, reconstructed recommendation mean, the raw
  distribution `dist`) — now with ~4 months of history per ticker on first collection.

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
// today's point (event_time = today): info fields + reconstructed consensus
{"forward_eps":7.42,"trailing_eps":6.6,"target_mean_price":355.0,
 "recommendation_mean":2.383,"n_analysts":47,
 "dist":{"strongBuy":6,"buy":23,"hold":14,"sell":2,"strongSell":2}}
// a back-dated earlier month (event_time = ~30 days ago):
{"recommendation_mean":2.333,"n_analysts":48,
 "dist":{"strongBuy":7,"buy":23,"hold":15,"sell":1,"strongSell":2},
 "source":"recommendations_history"}
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
