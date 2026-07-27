# Reliability — why it kept failing, the root causes, and how you'll know now

Straight answers to "why do we keep failing / how can I trust this."

## Root causes (found, not guessed)

**1. The collector crash-looped on `database is locked` (the main flakiness).**
Many processes write the one SQLite file (the collector daemon, the dashboard, S2/S3 jobs,
manual scripts). SQLite allows one writer; without WAL any concurrent access threw
`OperationalError: database is locked`, which was **uncaught** and killed the daemon.
launchd restarted it, it did a few ticks, died again — so it *looked* alive but barely
progressed. **Root cause: shared multi-writer SQLite with no WAL + an unguarded loop.**
Fix: **WAL + 60 s busy-timeout on every connection** (writers wait instead of erroring) and
a **crash-proof `run_forever`** (any tick error is logged and recovered, never fatal).

**2. Daily prices went stale because bars were switched to HOURLY.**
The Polygon **basic plan does not serve current intraday** — a request for hourly bars
through today returned `status: DELAYED`, ~1069 bars **ending months ago** (Oct 2025). So
after the hourly switch, the daily `price.close` projection couldn't advance and the model
was anchored on stale data. **Root cause: the plan can't deliver current hourly data.**
Fix: **bars reverted to DAILY aggregates** (`/range/1/day/`), which the basic plan serves
current (subject to weekend + ~1-day EOD delay). Intraday features are best-effort and
blocked on the plan (needs a paid tier).

**3. A code bug crashed `s3_multi.backfill`** — `RUNTIME_DIR` used but not imported
(`NameError`). Fixed.

**4. Stale "old predictions"** — the retired Ridge model's `predict.ret_*` (the P≈0.5
outputs) were still in the store and on the DAG. Purged; everything now uses the deployed
big-move classifier (`predict.pbig_*` / `dir_1d`).

## How you'll know now — the health monitor (`health.py`, `/health`)

Nothing here requires trusting a person. Every number is read from the DB:

- **Heartbeat** — seconds since the last API call (`source_calls`). >15 min ⇒ `STALLED`.
- **Per-signal freshness** — last successful collection per kind vs. its cadence; a
  *critical* kind (bars/quote/macro/implied_move/analyst/statements) older than 2× its
  cadence ⇒ `DEGRADED` with a named alert.
- **Data recency** — newest `price.close` and `bars` date, flagged if beyond weekend+delay.
- **Standing errors** — per kind (e.g. `transcript` = paid/blocked, excluded from critical).

`/health` auto-refreshes every 30 s with a big verdict banner + the freshness table + an
**alert history** (state changes are recorded to the `health_alerts` SQL table).

**Automatic alerting**: `com.stockpredictor.health` (launchd, every 5 min) runs
`python3 -m health alert` — on any transition into `DEGRADED`/`STALLED` (or recovery) it
records the alert and fires a **macOS notification**. CLI exit code is 0/1/2 (OK/degraded/
stalled) so it's also cron-alertable. (Email can be added if you provide an app password.)

## Verify it yourself
```
python3 -m health            # objective report, exit code = verdict
open http://localhost:8899/health
```
Both daemons (`com.stockpredictor.collector`, `.dashboard`) and the health checker are
launchd agents — independent of any editor session and auto-restarted.
