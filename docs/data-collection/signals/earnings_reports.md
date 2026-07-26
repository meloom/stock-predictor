# Signal: `earnings_reports` — reported quarterly results

| | |
|---|---|
| **Collector kind** | `earn_report` |
| **Source** | yfinance earnings history (EPS estimate vs reported, revenue, net income) |
| **Frequency** | `event` · daily poll (priority 46) · collects **every reported quarter** (~4–8/stock, back years), not just the latest |
| **Typed table** | `earnings_reports` |
| **S1 raw feature (projection)** | `earnings.report_raw` |
| **Source timestamp column** | `report_date` (actual announcement date) |

## Downstream consumers
- `earnings.analysis` (S2, kind `earn_analysis`) — beat/miss, surprise %, revenue YoY.
- Earnings-surprise features into S3.

> **S1/S2 boundary:** downloading the report is S1. *Analysing* it (beat/miss, YoY) is
> S2 (`_h_earn_analysis` reads this raw row and writes `earnings.analysis`). They must
> not be mixed.

## Schema (`earnings_reports`)
```sql
CREATE TABLE earnings_reports (
  ticker TEXT, eps_estimate REAL, eps_reported REAL, surprise_pct REAL,
  revenue REAL, net_income REAL, revenue_year_ago REAL,
  report_date TEXT,            -- SOURCE timestamp: announcement date
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, report_date)
);
```

## Example collected raw data (real row)
```json
{"ticker":"AAPL","eps_estimate":1.94,"eps_reported":2.01,"surprise_pct":3.46,
 "revenue":111184000000.0,"net_income":29578000000.0,"revenue_year_ago":95359000000.0,
 "report_date":"2026-04-30","ingested_at":"2026-07-26T00:07:07.49Z"}
```

## After processing (S2 → `earnings.analysis`)
```sql
SELECT ticker, report_date,
       CASE WHEN eps_reported >= eps_estimate THEN 'beat' ELSE 'miss' END AS beat_miss,
       surprise_pct,
       revenue / NULLIF(revenue_year_ago,0) - 1.0 AS revenue_yoy_pct
FROM earnings_reports WHERE ticker=:T ORDER BY report_date DESC LIMIT 1;
-- e.g. AAPL 2026-04-30 -> beat, surprise 3.46%, revenue_yoy 16.8%
```

## Coverage expectation & missing-data check
Event mode: collect every quarter yfinance returns (verified ~7.3 rows/ticker live;
AAPL back to 2020). Coverage = tickers covered + records/ticker + span — a ticker showing
only 1 quarter is a gap (surfaced in the dashboard's `recs (~N/ticker)` detail).
