# Signal: `options_implied` — ATM straddle / implied move

| | |
|---|---|
| **Collector kind** | `implied_move` |
| **Source** | Polygon `/v3/reference/options/contracts` + `/v2/aggs/.../prev` (ATM call+put) |
| **Frequency** | `snapshot` · daily poll (priority 25) |
| **Typed table** | `options_implied` |
| **S1 raw features** | `opt.implied_move`, `opt.straddle_pct`, `opt.expiry` |
| **Source timestamp column** | `snap_date` |

## Downstream consumers
- Event-risk sizing around earnings; feeds `alpha.event_risk` (S4).

## Schema (`options_implied`)
```sql
CREATE TABLE options_implied (
  ticker TEXT, underlying REAL, expiry TEXT, atm_call REAL, atm_put REAL,
  straddle_pct REAL, implied_move REAL,
  snap_date TEXT,              -- SOURCE timestamp: snapshot date
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, snap_date)
);
```

## Example collected raw data
> **Live gap flagged by coverage:** `options_implied` currently has **0 rows** — the
> handler has not yet landed a typed row. Expected shape once collecting:
```json
{"ticker":"AAPL","underlying":333.02,"expiry":"2026-08-01","atm_call":6.10,"atm_put":5.85,
 "straddle_pct":0.036,"implied_move":0.031,"snap_date":"2026-07-24","ingested_at":"…"}
```
`implied_move ≈ 0.85 × straddle/underlying`; `straddle_pct = (atm_call+atm_put)/underlying`.

## After processing (S4 event risk)
```sql
-- expected earnings-move magnitude for position sizing
SELECT implied_move, expiry FROM options_implied WHERE ticker=:T
ORDER BY snap_date DESC LIMIT 1;
```

## Coverage expectation & missing-data check
Snapshot mode (Polygon basic plan gives current chain only). Coverage = tickers with a
current snapshot. A dense implied-move *history* needs a paid options-history vendor
(ORATS/ORATS-style — see `docs/DATA_SOURCES.md`).
