# Signal: `analyst_revisions` — up/downgrade history

| | |
|---|---|
| **Collector kind** | `analyst_revisions` |
| **Source** | yfinance `upgrades_downgrades` (rolling history of firm actions) |
| **Mode / cadence** | `rolling` · daily (priority 41) |
| **Typed table** | `analyst_revisions` |
| **S1 raw feature** | `analyst.revisions_raw` |
| **Source timestamp column** | `revision_date` |
| **Row grain** | ONE ROW PER REVISION (not a JSON list) |

## Downstream consumers
- Up/downgrade drift and analyst-sentiment features.

## Schema (`analyst_revisions`)
```sql
CREATE TABLE analyst_revisions (
  ticker TEXT, firm TEXT, action TEXT,      -- action ∈ up|down|init|main|reit
  from_grade TEXT, to_grade TEXT,
  revision_date TEXT,          -- SOURCE timestamp: revision date
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, revision_date, firm, action)
);
```

## Example collected raw data (real row)
```json
{"ticker":"AAPL","firm":"Morgan Stanley","action":"main","from_grade":"Overweight",
 "to_grade":"Overweight","revision_date":"2026-07-23","ingested_at":"2026-07-26T00:07:05.97Z"}
```

## After processing
```sql
-- 90-day net revision drift: upgrades minus downgrades
SELECT SUM(CASE action WHEN 'up' THEN 1 WHEN 'down' THEN -1 ELSE 0 END) AS net_revisions_90d
FROM analyst_revisions
WHERE ticker=:T AND revision_date >= date('now','-90 day');
```

## Coverage expectation & missing-data check
Rolling mode: yfinance returns a long history (AAPL sample spans 2012→2026, 969 rows).
Coverage = tickers with ≥1 revision + record count + span. **Live gap:** typed table
holds AAPL only — universe backfill pending kickstart.
