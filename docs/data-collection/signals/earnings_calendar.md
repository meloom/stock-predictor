# Signal: `earnings_calendar` — next scheduled earnings date/time

| | |
|---|---|
| **Collector kind** | `earn_date` |
| **Source** | yfinance earnings calendar (next report datetime) |
| **Mode / cadence** | `snapshot` · daily (priority 45) |
| **Typed table** | `earnings_calendar` |
| **S1 raw feature (projection)** | `earnings.next_date` |
| **Source timestamp column** | `snap_date` (the day we observed this next-date) |

## Downstream consumers
- `calendar.days_to_earnings` (S2, kind `days_to_earn`) → `alpha.event_risk` (S4).

> **S1/S2 boundary:** S1 collects the **exact next earnings date/time**.
> `days_to_earnings` is a *derived* S2 signal (`next_earnings_ts − today`). Do not
> compute the countdown in S1.

## Schema (`earnings_calendar`)
```sql
CREATE TABLE earnings_calendar (
  ticker TEXT,
  next_earnings_ts TEXT,       -- the scheduled report datetime (with tz)
  snap_date TEXT,              -- SOURCE timestamp: observation day
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, snap_date)
);
```

## Example collected raw data (real row)
```json
{"ticker":"AAPL","next_earnings_ts":"2026-07-30T16:00:00-04:00",
 "snap_date":"2026-07-26","ingested_at":"2026-07-26T00:07:07.54Z"}
```

## After processing (S2 → `calendar.days_to_earnings`)
```sql
SELECT ticker,
       CAST(julianday(date(next_earnings_ts)) - julianday(:asof) AS INT) AS days_to_earnings
FROM earnings_calendar WHERE ticker=:T ORDER BY snap_date DESC LIMIT 1;
-- e.g. next_earnings_ts 2026-07-30, asof 2026-07-26 -> 4
```

## Coverage expectation & missing-data check
Snapshot mode: coverage = tickers with a current next-date; refreshed daily as the date
approaches/rolls. Missing = tickers with no row or stale `snap_date`.
