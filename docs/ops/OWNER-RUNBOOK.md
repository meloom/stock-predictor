# Data collector — owner runbook (check & resume it yourself)

You own this. Three commands, no Claude needed. Run them from the repo root with the venv
active (`source venv/bin/activate; export PYTHONPATH=$PWD/src`).

## 1. Is it healthy? — `doctor`
```
python3 -m collector doctor
```
Prints one of three verdicts in plain English:

| Verdict | Meaning | Action |
|---|---|---|
| **HEALTHY** | collecting now, last success < 30 min ago | nothing — it's working |
| **IDLE-OFFHOURS** | outside the 09:00–23:00 London window; only backfilling gaps | **nothing — this is normal.** It goes quiet overnight *by design* |
| **STALLED** | in-window but no success for a long time | resume it (below) |

It also prints: daemon running (True/False), last success time, the window, tasks due,
and the exact resume command. Exit code is `0` unless STALLED (so you can alert on it).

## 2. Resume it — one command
```
launchctl kickstart -k gui/$(id -u)/com.stockpredictor.collector
```
This restarts the launchd daemon cleanly. It self-reconciles on startup (re-arms any
missing work) and keeps running independently of any terminal or Claude session.

## 3. See the raw activity — the dashboard
`http://localhost:8899/data-collection` — the **"Download rate & failures per source"**
chart shows rows collected per hour (from the immutable event log — it does *not* lie about
timing anymore). Green = collected, red strip = a real failure that hour.

---

## Why "it looks stopped" (and usually isn't)

- **Overnight it goes quiet on purpose.** Per policy, new jobs only fire 09:00–23:00
  London; outside that it only completes backfill. Zero successes overnight ≠ dead —
  `doctor` will say `IDLE-OFFHOURS`.
- **`transcript` is a paid, blocked source — now disabled** (`config/collection.json`,
  `"enabled": false`). It used to fail thousands of times and made everything look broken.
  To turn it on once you have a key: set `enabled: true` and put `TRANSCRIPT_API_KEY` in
  `~/.credentials`.

## Independence — what keeps it running without a session

Three launchd agents (auto-restart, survive logout, no terminal needed):
- `com.stockpredictor.collector` — the collector daemon
- `com.stockpredictor.dashboard` — the dashboards
- `com.stockpredictor.health` — health checks every 5 min (macOS notification on failure)

Check they're loaded: `launchctl list | grep stockpredictor`.

## Change what/how-often we collect — no code
Edit **`config/collection.json`**: `interval_sec` per signal, the `collection_window_local`,
`live_only`, or `enabled`. `reconcile()` applies it to live tasks within 5 minutes — no
restart, no code change.
