# S1 Data Collection — Scheduling & Coverage Design

This document is the contract for the **data-collection module** (`src/collector.py`,
`src/schema.py`, `src/s1_data.py`). It describes (1) how collection is **scheduled**,
(2) how we **confirm data coverage** and detect missing data *from a start date until
now*, and (3) links to a **per-signal schema file** for every signal the downstream
steps consume. If a downstream feature exists, its raw input must appear in the index
below — that is the completeness guarantee.

---

## 1. Scheduling design

### 1.1 Queue-driven, always-on, source-rate-limited

Collection is a **persistent priority queue**, not a cron of ad-hoc fetches. One row
per `(source, kind, scope)` in the `collection_tasks` table; a single daemon
(`collector run`, a launchd LaunchAgent) drains it forever, independent of any Claude
session.

```
collection_tasks(task_id PK, source, kind, scope, priority, interval_sec,
                 next_due, status, attempts, last_error, last_ok, updated_at)
source_calls(source, ts)        -- rolling-window rate-limit ledger
```

**One tick = at most one unit of work.** `tick()` selects every *due* task
(`status='pending' AND next_due<=now`) ordered by `priority ASC, next_due ASC`, then
runs the first one whose **source has quota**. There is deliberately **no `LIMIT`** on
the candidate query: a rate-limited source sitting at the top of the priority order
(e.g. 109 Polygon tasks) must not starve a fast source below it — when Polygon's quota
is spent the worker falls through to the next source that still has quota.

**Rate limits** are enforced per source over a rolling window, strictly:

| Source     | Limit        | Why                                             |
|------------|--------------|-------------------------------------------------|
| `yfinance` | 30 / 60s     | gentle — yfinance throttles bursts to empty     |
| `polygon`  | 5 / 60s      | basic-plan hard cap                             |
| `process`  | 10000 / 60s  | local CPU (S2 derived signals, no network)      |

A task only starts if `available(source) >= est_calls`. On success it is rescheduled
`next_due = now + interval_sec`. On failure it backs off (attempts++, `next_due`
pushed out) and records `last_error` — an errored task **does not** count as collected.

### 1.2 Per-signal cadence — driven by `config/collection.json`

Poll cadence is **configuration, not code.** `config/collection.json` is the single
source of truth; `collector.py` reads it at startup and `reconcile()` re-syncs every
live task to it every 5 minutes — so **editing the JSON changes cadence with no code
change and no restart wait.**

```jsonc
{
  "collection_window_local": [9, 23],     // only fire NEW jobs 09:00–23:00 local (London)
  "live_only": ["quote"],                 // live-only signals collected first, always
  "signals": {
    "quote":  {"interval_sec": 3600, "fresh_sla": [3600, 7200]},
    "bars":   {"interval_sec": 3600},
    "short":  {"interval_sec": 3600},
    ...
  }
}
```

Current cadences:

| Signal(s) | Poll cadence | Why |
|-----------|--------------|-----|
| `quote` (LIVE) | **1 h** | live session-aware price — highest priority, never recoverable later |
| `bars` | **1 h** | OHLCV incl. volume (daily EOD on the basic plan; hourly if the entitlement allows) |
| `short`, `statements`, `analyst` | **1 h** | upstream updates bi-monthly/quarterly, but polled hourly to catch a new print fast; handlers **dedup** so identical values aren't re-stored |
| `insider`, `sec_filings` | **1 d** | event-driven |
| `macro`, `implied_move`, `analyst_revisions`, `earn_date`, `earn_report`, `xbrl`, `transcript` | **1 d** | daily default |

### 1.2b Scheduling policy — live-first, latest, then backfill; windowed

`tick()` selects due tasks in this order (see the `ORDER BY`):

1. **LIVE-only** signals (`live_only` in config) — first, because they can never be
   recovered later.
2. **Latest refresh** — already-collected tasks coming due (`last_ok` set): the newest
   data for each signal.
3. **Backfill** — never-collected gaps (`last_ok IS NULL`): filled last, with spare
   capacity.

**Collection window.** New routine jobs fire only inside `collection_window_local`
(default **09:00–23:00**, machine-local = London; env `STOCK_COLLECT_WINDOW="9-23"`
overrides). **Outside the window we shoot nothing new** — `tick()` runs *only* gap
backfill (`last_ok IS NULL`), so idle overnight hours are spent completing earlier
missing signals rather than re-polling unchanged data.

- **`event`** signals collect **all new records from the last-collected watermark to
  now** (e.g. earnings = every reported quarter). Coverage = "covered + record count +
  span", never a fake fixed depth.
- **`snapshot`** is point-in-time state re-sampled each poll; history accrues forward.

### 1.3 Declarative self-reconciliation (add a signal → auto-backfill)

`reconcile()` is the single source of truth for "what should exist." For every declared
`(kind, scope)`:

* **brand-new** (no task yet) → enqueue as a **prioritized backfill** (`priority-1000`);
* **never successfully collected** and not already queued → re-arm as prioritized backfill.

It is **idempotent** and the daemon runs it **on startup and every 5 minutes**.
Consequence — and this is the design promise: **declaring a new signal is all it takes.**
`register_kind("short", …, backfill_days=90)` plus `reconcile()` automatically injects a
90-day-scoped prioritized backfill for every ticker into the queue. No manual `seed`, no
ad-hoc fetch. Adding a ticker (`add_ticker`) likewise enqueues every ticker-scoped kind
as a prioritized backfill.

### 1.4 How far back — `backfill_days` and the three collection modes

`backfill_days` (default **90**) declares the intended history window. Whether that window
can actually be *filled* depends on the source, captured by the kind's **mode**:

* **`history`** — source serves the full daily range (Polygon aggregates, yfinance macro
  history). The backfill genuinely reaches back `backfill_days`.
* **`snapshot`** — source exposes only *now* (yfinance quote / short / analyst / next-earnings
  date; Polygon current implied move). **Cannot be backfilled**; true history accrues only
  going forward from first collection. Reporting this as "100% done" would be a lie — see §2.
* **`rolling`** — source returns a rolling *list* of past events (earnings reports,
  statements, analyst up/downgrades, insider transactions). We get whatever history the
  source chooses to return (often years), then accrue new events forward.

---

## 2. Coverage design — confirming data, detecting missing from-when-until-now

The old dashboard measured "did each ticker's task run once" — a **presence** check that
showed a green 100% even when we held a single snapshot. Coverage is now measured
**against expectation, per mode** (`Collector.coverage_report()` → `overall` + per-kind):

| Mode      | Coverage % means                                   | "Missing" is detected by                                             |
|-----------|----------------------------------------------------|---------------------------------------------------------------------|
| history   | fraction of the expected **trading-day window** actually stored (breadth × depth) | expected trading days in `[start, today]` **minus** distinct dates present per ticker |
| snapshot  | tickers holding a **current** value (history accrues forward) | tickers with **no** snapshot row; staleness of `ingested_at`         |
| rolling   | tickers **covered** + record count + date span     | tickers with **zero** event rows; span that doesn't reach `[start]`  |

### 2.1 Depth measurement

`schema.TypedStore.depth(table, cap)` returns, per entity, the count of **distinct
source-timestamps** stored. A snapshot collected once has depth 1; a fully backfilled
daily series has depth ≈ trading days. `capped_sum / (entities × cap)` is the honest
"how full is the window" fraction that drives the `history` %.

### 2.2 Gap detection from a start date until now

For a **history** signal, the missing-data check is an explicit set difference against a
trading calendar. Conceptually, per ticker `T` over `[start, today]`:

```sql
-- dates we SHOULD have (a trading-day calendar) vs dates we DO have
WITH have AS (SELECT DISTINCT date FROM bars WHERE ticker = :T AND date >= :start)
SELECT :T AS ticker,
       (SELECT COUNT(*) FROM trading_days WHERE d BETWEEN :start AND :today) AS want_days,
       (SELECT COUNT(*) FROM have)                                           AS have_days,
       (SELECT COUNT(*) FROM trading_days td
          WHERE td.d BETWEEN :start AND :today
            AND td.d NOT IN (SELECT date FROM have))                         AS missing_days;
```

For **rolling / snapshot** signals the check is coverage-of-entities plus freshness:

```sql
-- tickers with NO row at all (hard gap), and the newest we hold (staleness)
SELECT :T AS ticker,
       (SELECT COUNT(*) FROM short_interest WHERE ticker = :T)        AS rows_held,
       (SELECT MAX(settlement_date) FROM short_interest WHERE ticker = :T) AS newest_held;
```

The dashboard's **Coverage vs expectation** panel renders exactly these numbers per kind
(depth `have/expected days`, or `tickers covered · records · span`), and the
**Coverage & freshness** heatmap colours each `(ticker × signal)` cell by `ingested_at`
age (`≤24h / ≤7d / >7d / missing`) so a from-when-until-now gap is visible per cell.

`collector coverage` prints the same honest per-kind report on the CLI (no dashboard
needed), for use in health checks / cron.

### 2.3 Known live gaps (as of this writing — surfaced *by* the honest metric)

The metric immediately exposed real gaps that the old presence bar hid:

* **Typed tables populated for AAPL only** (`entities=1`). The queue marked tickers
  "collected" via the legacy `feature_values` projection, but the **typed** backfill has
  not yet swept the universe. Fix: kickstart the daemon on current code so `reconcile()`
  re-arms the typed backfill for all tickers.
* **`options_implied` = 0 rows** — the implied-move handler has not landed a typed row yet.
* **`macro.spy_close` value = NULL** — macro handler stored a null for SPY close (bug to fix
  in `_h_macro`).

These are tracked as coverage debt, not hidden behind a green bar.

---

## 3. Signal index — the complete downstream-consumed list

Every signal any later step (S2 features → S3 predictors → S4 alpha → modeling) consumes,
mapped to the S1 raw signal that must be collected. **If a row here is missing its raw
input, that is a collection gap.** One schema file per signal family under `signals/`.

| # | Signal family (file) | S1 raw feature(s) | Typed table | Mode | Downstream consumers |
|---|----------------------|-------------------|-------------|------|----------------------|
| 1 | [bars](signals/bars.md) | `price.close`, `price.volume` | `bars` | history | `tech.rsi14/mom5/mom20/hvol20/vr20/ret_lag1..7`, `xh.new_high_flag`, `xh.above_hi_streak`, `fund.market_cap`, all `fund.*` price terms, S3 predictors |
| 2 | [quotes](signals/quotes.md) | `price.current` | `quotes` | snapshot | live pricing / execution, intraday reference price |
| 3 | [macro](signals/macro.md) | `macro.vix`, `macro.spy_close`, `macro.yield10y` | `macro` | history | `alpha.regime`, `alpha.event_risk`, market context in S3/S4 |
| 4 | [short_interest](signals/short_interest.md) | `short.shares`, `short.pct_float`, `short.days_to_cover`, `short.change_pct` | `short_interest` | snapshot | near-earnings crowding module (squeeze fuel) |
| 5 | [options_implied](signals/options_implied.md) | `opt.implied_move`, `opt.straddle_pct`, `opt.expiry` | `options_implied` | snapshot | event-risk sizing, `alpha.event_risk` |
| 6 | [earnings_reports](signals/earnings_reports.md) | `earnings.report_raw` | `earnings_reports` | event | `earnings.analysis` (S2), beat/miss & surprise features |
| 6b | [sec_filings / xbrl / transcript](signals/sec_filings.md) | filing text, XBRL facts | `sec_filings`, `xbrl_financials`, `transcripts` | event | S2 NLP layer (guidance, tone, surprise-vs-narrative); authoritative value factors |
| 7 | [earnings_calendar](signals/earnings_calendar.md) | `earnings.next_date` | `earnings_calendar` | snapshot | `calendar.days_to_earnings` (S2) → `alpha.event_risk` |
| 8 | [analyst_snapshot](signals/analyst_snapshot.md) | `fundamental.analyst_snapshot` | *(feature_values)* | snapshot | consensus level; revision series accrues forward |
| 9 | [analyst_revisions](signals/analyst_revisions.md) | `analyst.revisions_raw` | `analyst_revisions` | rolling | up/downgrade drift, sentiment features |
| 10 | [insider](signals/insider.md) | `insider.transactions_raw` | `insider_transactions` | rolling | insider buy/sell pressure features |
| 11 | [fundamentals](signals/fundamentals.md) | `fundamental.statements`, `fundamental.shares_outstanding` | `fundamentals` | rolling | `fund.earnings_yield/book_to_price/fcf_yield/roe/net_margin/gross_profitability` → `xsec.rank_*` |

### Derived (S2+) — computed *from* the above, NOT collected
`tech.*` (from bars) · `fund.*` (from fundamentals + bars) · `xsec.rank_*` (cross-section of
`fund.*`) · `xh.*` (from bars) · `calendar.days_to_earnings` (from earnings_calendar) ·
`earnings.analysis` (from earnings_reports) · `predict.*` (S3, from all features) ·
`alpha.*` (S4, from `predict.eod_return` + macro + calendar). These are documented inside
each parent signal file under "After processing (S2)".
