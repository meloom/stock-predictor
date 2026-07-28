# Signal: `bars` — daily OHLCV

| | |
|---|---|
| **Collector kind** | `bars` |
| **Source** | Polygon `/v2/aggs/ticker/{T}/range/1/day/{from}/{to}` (daily; reliable on the basic plan) |
| **Frequency** | **daily** · full range re-fetched `COLLECTION_START` (2025-07-01) → today every run, priority 20 |
| **Typed table** | `bars` (one row per **trading day**, keyed by `bar_ts` = a bare `YYYY-MM-DD`) |
| **S1 raw features (projection)** | `price.close`, `price.volume` per day → `feature_values` (so S2/S3 read a clean daily series) |
| **Source timestamp column** | `bar_ts` — a **date** for daily rows (`2026-07-27`); older intraday rows carry a full `…T14:00:00+00:00` timestamp |

## ⚠️ Why DAILY and not hourly (the honest, tested reason)
The original design (and an earlier version of this doc) called for **hourly** bars via
`/range/1/hour/`. We collected them Jul–Oct 2025 (~157k intraday rows). **The Polygon
basic plan no longer serves a complete intraday series.** Live-tested 2026-07-28:

| Request | Result |
|---|---|
| hourly, last 10 days (`…/range/1/hour/2026-07-18/2026-07-28`) | `status: DELAYED`, **1 bar** returned |
| hourly, an older single day (`2026-06-15`) | `status: OK`, **3 bars**, all 08:00–11:00 UTC (pre-market only) |
| **daily** (`…/range/1/day/…`) | `status: OK`, **complete** series to yesterday's EOD |

So an hourly request returns either delayed (1 bar) or a tiny pre-market-only subset —
**not a usable within-day series.** Daily returns complete data with the normal ~1-day
EOD lag. The collector therefore fetches **daily** (`_h_bars_polygon`). Consequences:
- The newest `bar_ts` is **yesterday's date** (EOD lag) — this is expected, not a stall.
- There is **no 9am/10am/11am price** in a daily row, so an *hourly* EOD predictor is not
  buildable from this plan. It needs a **paid intraday entitlement** (or the plan's
  intraday access restored — it worked Jul–Oct 2025, then degraded).
- Historical intraday rows (bar_ts with a `T`time) still exist in the table for
  Jul 2025 → Feb 2026 and can be used for *backtests* of that window only.

## Downstream consumers (must not break)
- `tech.rsi14`, `tech.mom5/mom20`, `tech.hvol20`, `tech.vr20`, `tech.ret_lag1..7` (S2)
- `xh.*` (new-high / distance-to-high / trailing returns) (S2)
- `fund.market_cap` and every `fund.*` ratio's price term (S2)
- All S3 predictors (price/return features) → S4 alpha (`predict.dir_1d`, `predict.pbig_*`)

## Schema (`bars`)
```sql
CREATE TABLE bars (
  ticker TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
  bar_ts TEXT,                         -- SOURCE timestamp: 'YYYY-MM-DD' (daily) or full ISO (legacy intraday)
  ingested_at TEXT NOT NULL,           -- when WE collected it
  PRIMARY KEY (ticker, bar_ts)
);
```

## Example collected raw data (real daily row)
```json
{"ticker":"AAPL","open":334.54,"high":339.57,"low":334.02,"close":336.91,
 "volume":49604297.0,"bar_ts":"2026-07-27","ingested_at":"2026-07-28T20:04:59Z"}
```

## Coverage expectation & missing-data check
Expected depth = **trading days** since `COLLECTION_START` (NYSE calendar, weekends +
holidays excluded — see `trading_days()`). A market-open day with no `bars` row for a
ticker is a real gap; the newest day lagging by ~1 session is the normal EOD delay.
Collection re-fetches the full range each run, so history self-heals; the
`collection_events` log records every attempt (ok/fail) for audit.
