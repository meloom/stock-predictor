# Data sources — near-earnings signals (what's free, what costs money)

## Running the always-on collector (`src/collector.py`)

S1 is a queue-driven collector: a persistent task queue (`collection_tasks` in the
store DB), a per-source rate limiter, and a worker that drains it — strictly pacing
each source (yfinance polite, **Polygon 5/min** for the basic plan). Recurring tasks
keep data fresh; `add-ticker` / a new source enqueue prioritized backfills.

```
python -m collector seed                 # ensure recurring tasks for the universe
python -m collector add-ticker NVDA      # prioritized backfill for a new ticker
python -m collector drain --seconds 55   # one bounded pass (for a per-minute cron)
python -m collector run                  # long-running daemon
python -m collector status               # queue + quota snapshot
python -m collector_dashboard out.html   # render the coverage/backfill dashboard
```

**Cron (self-healing, respects limits):** run a bounded drain every minute — each
run consumes only that minute's quota, then exits:
```
* * * * * cd /path/to/stock-predictor && PYTHONPATH=src venv/bin/python -m collector drain --seconds 55 >> runtime/logs/collector.log 2>&1
```
Secrets (e.g. `POLYGON_API_KEY`) live in `~/.credentials` (chmod 600), read via
`collector.load_secret` — never committed.

---


The near-earnings module (see `modeling/EARNINGS_RESEARCH.md`) needs signals that
are NOT in our current price/volume/fundamental feeds. Status and cost of each.

## ✅ Implemented (free)

### Short interest — `short.*` (S1)
- **Source:** yfinance `.info` (`sharesShort`, `sharesShortPriorMonth`,
  `dateShortInterest`, `shortRatio`, `shortPercentOfFloat`) → the latest **FINRA
  bi-monthly** print. Ingested in `s1_data.fetch_short_interest` +
  `run_daily_ingestion(fetch_short=...)`.
- **Features:** `short.pct_float`, `short.days_to_cover`, `short.change_pct`
  (buildup/covering), `short.shares`. Written at **event_time = the settlement
  date** the print refers to (PIT-correct placement).
- **Limitations (honest):**
  1. **Bi-monthly, not daily** — FINRA publishes ~24 prints/year (near the 15th and
     month-end). It's a slow crowding signal, not a daily-timing one.
  2. **Dissemination lag ~8 business days** — FINRA releases each print ~8 business
     days AFTER settlement. We store at the settlement date; a *strict* as-of
     backtest should lag reads by the dissemination gap. yfinance gives no
     dissemination date, so we approximate.
  3. **No deep history** — yfinance returns only the current + prior print, so we
     **accrue real history going forward** (same pattern as `analyst_snapshot`). A
     full historical, dissemination-dated series needs the FINRA archive (below).

### Analyst-revision momentum — `analyst` block (modeling)
- yfinance `upgrades_downgrades` (dated grade changes). Free, PIT-safe. Tested:
  weak near-earnings directional tilt (see EARNINGS_RESEARCH.md); not promoted.

## 💵 To do properly — PAID / higher-effort (tracked: task #14)

### 1. Full historical short interest (dissemination-dated) — LOW cost
- **Free/official:** [FINRA Equity Short Interest archive](https://www.finra.org/finra-data/browse-catalog/equity-short-interest/data)
  — downloadable bi-monthly files with settlement + dissemination dates. **$0**, but
  needs a file downloader/parser + FINRA data account. This upgrades our forward-only
  yfinance series to a full backtestable history. **Do this first — it's free.**
- **Daily short interest (modeled):** **S3 Partners** or **Ortex** estimate *daily*
  short interest from securities-lending/borrow data. Paid (typically **~$100–1,000+/mo**
  depending on tier/coverage). Better signal than bi-monthly, but it's a model, not
  the FINRA truth.

### 2. Options-implied move + skew — MEDIUM cost, the best MAGNITUDE signal
Needs **historical EOD option chains or an IV surface** as-of each date (yfinance
`option_chain` is current-only → useless for backtest). Compute
`implied_move ≈ 0.85 × (ATM_call + ATM_put)/underlying` for the first expiry after
earnings; event-vol via the term-structure kink; skew = 25Δ put IV − call IV.
- **[Polygon.io](https://polygon.io)** — raw historical options quotes/aggregates,
  developer-friendly. **~$29–199/mo** (options tier). Cheapest credible path; you
  compute IV/straddle yourself.
- **[ORATS](https://orats.com)** — *earnings-focused*: pre-computed IV surfaces,
  **earnings implied move**, skew, dividends, 25y EOD + a hosted backtester. Best fit
  for this exact use case. **~$200–500+/mo** (plans vary).
- **[OptionMetrics IvyDB](https://optionmetrics.com)** — academic standard, cleanest
  IV/greeks; **institutional pricing** (contact sales; typically $$$$).
- **Others:** IVolatility, Cboe DataShop (per-dataset), EOD Historical Data (cheaper,
  EOD summaries).

### 3. Earnings-call transcript NLP — build cost, highest ceiling
Transcripts (e.g. via a provider or scraping) + an LLM/FinBERT tone & guidance-change
extractor. Best for **PEAD** (day-after drift), not the initial gap. Highest effort;
do last.

## Recommendation / sequencing
1. **FINRA archive** for full historical short interest — **$0**, unlocks backtesting
   the `short.*` features we just wired in.
2. **Polygon** (~$29–199/mo) for options-implied move → the single best magnitude
   signal for the near-earnings module's Model-1 head.
3. ORATS if we want earnings implied-move pre-computed and a hosted backtester.
4. Transcript NLP last.

All of these feed the **near-earnings module / Model-1 magnitude head**, NOT the daily
directional model (which these event signals were shown to *hurt* — see
EARNINGS_RESEARCH.md).
