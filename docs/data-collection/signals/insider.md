# Signal: `insider` — insider transactions

| | |
|---|---|
| **Collector kind** | `insider` |
| **Source** | yfinance `insider_transactions` (rolling history) |
| **Frequency** | `event` · 3-day poll (priority 44) · collects all transactions the source returns |
| **Typed table** | `insider_transactions` |
| **S1 raw feature** | `insider.transactions_raw` |
| **Source timestamp column** | `txn_date` |
| **Row grain** | ONE ROW PER TRANSACTION |

## Downstream consumers
- Insider buy/sell pressure features (net buying, officer vs director).

## Schema (`insider_transactions`)
```sql
CREATE TABLE insider_transactions (
  ticker TEXT, value REAL, shares REAL, position TEXT, insider TEXT, is_sale REAL,
  txn_date TEXT,               -- SOURCE timestamp: transaction date
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, txn_date, insider, value)
);
```

## Example collected raw data (real row)
```json
{"ticker":"AAPL","value":34236.0,"shares":116.0,"position":"Officer",
 "insider":"BORDERS BEN","is_sale":1.0,"txn_date":"2026-06-16",
 "ingested_at":"2026-07-26T00:07:05.90Z"}
```

## After processing
```sql
-- net insider dollar flow over 90 days (buys positive, sales negative)
SELECT SUM(CASE WHEN is_sale=1 THEN -value ELSE value END) AS net_insider_flow_90d,
       SUM(CASE WHEN is_sale=0 THEN 1 ELSE 0 END)          AS n_buys_90d
FROM insider_transactions
WHERE ticker=:T AND txn_date >= date('now','-90 day');
```

## Coverage expectation & missing-data check
Rolling mode: yfinance returns up to ~2 years (AAPL sample 2024→2026, 78 rows). Coverage
= tickers with ≥1 txn + count + span. **Live gap:** typed table holds AAPL only.
