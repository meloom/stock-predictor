# System Design — stock-predictor

Status: **design baseline for the clean rebuild** (2026-07-24).
Predecessor: a working prototype (now archived off-repo) that traded real
capital and paid for every lesson encoded here. Where this doc states a rule,
there is a dated, real-money incident behind it.

**Sync rule: this document is the source of truth and changes in the same
commit as the behavior it describes.** A design doc that trails the code is
how the predecessor ended up with "non-negotiable" purge gaps that existed
only in documentation. Implementation-level decisions are recorded in the
*Implementation notes* blocks below as modules land.

---

## 1. Goal and honest constraints

- Long-only US-equity system, IBKR execution, small account (~$2K), daily cadence.
- **Aspirational** target: 10%/month. **Measured reality**: no alpha signal
  validated to date — the predecessor's technical-feature ranker showed
  Spearman IC ≈ 0 vs. market-excess returns on purged out-of-sample data,
  decile hit rates 48–54% (statistically flat), and two live days where its
  picks underperformed its own avoid-list. Therefore:
- **Prime directive: the system defaults to cash.** Live stock-picking is
  disabled until a signal passes the validation gate (§5). Execution, risk,
  and ops layers are built and kept live-ready in the meantime.
- Structural friction is real at this account size: ~$1/leg commission
  (0.2–0.6% one-way on typical positions) and whole-share granularity.
  Every design decision must survive those two numbers.

---

## 2. High-level architecture

```mermaid
flowchart TD
    subgraph DATA["S1 · Data Ingestion"]
        D1[Prices/volume OHLCV daily+intraday]
        D2[Macro: VIX, yields, DXY, Fed]
        D3[Earnings calendar]
        D4[Earnings-report signals - LLM grounded extraction]
    end

    subgraph SIG["S2 · Signal Generation"]
        F1[Technical features per ticker]
        F2[Cross-sectional ranks/z-scores]
        F3[Regime features - market level]
        F4[Fundamental signals: guidance, capex framing, adj-EPS surprise]
    end

    subgraph ALPHA["S3 · Alpha"]
        A1{"Regime gate: below threshold → CASH"}
        A2["Stock scorer — VALIDATION-GATED, currently disabled"]
        A3["Event-risk veto — earnings proximity, deterministic"]
    end

    subgraph PORT["S4 · Portfolio Construction"]
        P1[Confidence-tiered dollar targets]
        P2[Whole-share sizing + value-vs-target verification]
        P3[Concentration cap ≤25% per name]
    end

    subgraph EXEC["S5 · Execution"]
        E1[Position sync vs broker ground truth]
        E2[Limit orders only, RTH/AH offsets]
        E3[Fill verification loop]
        E4[Per-ticker cooldown after loss]
    end

    subgraph RISK["S6 · Intraday Risk Loop (15-min)"]
        R1[Hard stop -2%]
        R2[Time-stop: flat ±0.5% after 2h]
        R3[Soft-signal exits → LLM decision gate w/ web search]
        R4[Daily loss circuit breaker -2%]
        R5[EOD close-all]
    end

    subgraph REPORT["S7 · Reporting"]
        N1[Email on every fill]
        N2[Morning report: regime, ranks, earnings flags, honest calibration note]
        N3[Health checks + escalating alerts]
    end

    subgraph EVAL["S8 · Evaluation & Improvement"]
        V1[Nightly retrospective: root-cause → fix → backtest → gated deploy]
        V2[Prediction-quality tracking: daily IC vs full universe]
        V3[Rank calibration refresh - monthly]
    end

    DATA --> SIG --> ALPHA
    A1 -- "regime OK" --> A2 --> A3 --> PORT --> EXEC --> RISK --> REPORT
    A1 -- "regime weak" --> CASH[(Stay in cash)]
    RISK --> EVAL
    REPORT --> EVAL
    EVAL -. "validated changes only" .-> SIG
    EVAL -. "validated changes only" .-> ALPHA
```

**Validation gate overlay** (applies to every dashed edge and to S3-A2):
no component reaches live capital until it passes §5 in the exact
configuration it will run in. "Validated in isolation" is not validated —
the predecessor's exit-cascade deployed on isolated validation and lost
money at every threshold when run in the real pipeline.

---

## 3. Per-step specification

### S1 · Data Ingestion

| | |
|---|---|
| **Input** | Ticker universe (~140–150 names, price ≥ $25); yfinance OHLCV (daily + 5-min); macro series (VIX, 10Y, curve, DXY, Fed rate, FOMC calendar); earnings calendar (yfinance `earnings_dates`, requires `lxml` — silently degrades without it); grounded LLM extraction of the most recent earnings report per ticker (Claude API + real web search) |
| **Output** | (1) Immutable raw source data (OHLCV bars, macro series, raw LLM responses) retained for reproducibility; (2) **features registered in the feature store** (contract below) — everything downstream-consumable is a store feature, no bespoke side-channel artifacts. Namespaced registry, e.g. `price.*` (close, volume, intraday bars), `macro.*` (vix, yield10y, curve, …), `calendar.days_to_earnings`, `fundamental.*` (guidance_direction, capex_trend, capex_framing, adj_eps_surprise_pct, revenue_surprise_pct, net_signal, confidence) |
| **Metrics** | Data freshness (hours since last bar; alert > 24h on a trading day) · Coverage (tickers with today's data / universe; alert < 95%) · Registry conformance: % of downstream reads served from registered features (target 100%; any bespoke-artifact read is a defect) · Earnings-signal grounding rate (% of calls returning `has_recent_report` with cited evidence vs. errors) · LLM call budget (calls/day; cap and monitor — a duplicated compute path once silently doubled daily spend) |
| **Hard rules** | LLM extraction must be **grounded** (real search results) — an ungrounded classifier flip-flopped HIGH/LOW on a stable non-event and caused real whipsaw trades. Fails closed (`insufficient_data`), never guesses. Extended-hours prices come from fields that actually carry them — `fast_info.lastPrice` does not (stale-close bug, caught live). |

#### Feature store contract (consumed by S2–S8)

- **Registry**: every feature is declared before first write — name (namespaced),
  dtype, scope (per-ticker / market-level), source stage, update cadence, and
  its **point-in-time rule** (when the value becomes knowable). Unregistered
  writes are rejected.
- **Bitemporal keys**: values are stored keyed by *(feature, scope, event_time,
  ingested_at)*. `event_time` is what backtests join on (no lookahead by
  construction); `ingested_at` is what S8's lineage audit checks (a prediction
  reading values whose `ingested_at` predates the session is a stale-input
  defect). This makes the predecessor's undetected 22-cycle placeholder-value
  failure structurally impossible to miss.
- **Append-only, versioned**: corrections append a new `ingested_at` version;
  history is never rewritten, so any past prediction can be reproduced exactly
  from what was known at the time.
- **One read API for research and production**: backtests and the live trader
  read features through the same interface. No manifest-maintained feature
  lists, no separate research/production code paths — the train/serve skew
  that let the predecessor validate components in configurations that never
  matched live.

**Implementation notes (landed 2026-07-24)**
- `src/feature_store/store.py`: SQLite at `runtime/features.db` (stdlib,
  single file, zero infra — the contract is the point, the backend is
  deliberately boring). `register()` is idempotent but raises on spec change:
  semantic changes get a NEW feature name, meaning is never mutated under an
  old one. Reads: `read_asof(feature, scope, event_time, as_known_at)` and
  `read_panel(...)`; `as_known_at` filters on `ingested_at`, which is what
  makes backtests lookahead-impossible by construction. `freshness()` serves
  S8's staleness audit. Every write carries `trigger_id` (lineage → cost ledger).
- `src/common/trigger.py`: `Trigger` context manager mints `trigger_id`,
  appends to `runtime/logs/cost_ledger.jsonl` (per billable action) and
  `runtime/logs/runs.jsonl` (per run — **including crashed runs**, logged with
  `status="error"`; silence must never be indistinguishable from success).
  Ledger prices are estimates for regression detection; the billing dashboard
  stays ground truth for absolute spend.
- `src/data/registry.py`: the declared S1 feature set (`price.close`,
  `price.volume`, `macro.vix`, `macro.yield10y`, `macro.spy_close`,
  `calendar.days_to_earnings`, `fundamental.earnings_signal`) with per-feature
  point-in-time rules. This file IS the ingestion contract — the store rejects
  anything not declared here.
- `src/data/sources.py`: the only network I/O in the data layer (plus the LLM
  call in `earnings_signal.py`); everything else takes fetchers as injected
  callables so tests run offline. Unknown values return `None`, never sentinel
  numbers (the predecessor's `days_to_earnings=999` sentinel leaked into
  model features as a plausible-looking number).
- Runtime artifacts live under gitignored `runtime/` (override:
  `STOCK_PREDICTOR_RUNTIME`).

### S2 · Signal Generation

| | |
|---|---|
| **Input** | Feature store reads (`price.*`, `macro.*`, `calendar.*`, `fundamental.*`) |
| **Output** | **Derived features written back to the feature store** under their own namespaces — `tech.*` (RSI, BB, ATR, momentum 3–60d, volume ratios), `xsec.*` (per-day cross-sectional ranks/z-scores), `regime.*` (market-level) — same registry, bitemporal keys, and point-in-time rules as S1 outputs. Model-ready matrices are assembled from store reads at query time, not maintained as separate artifacts |
| **Metrics** | Feature NaN rate per column (alert on regression) · Lookahead audit: every feature reproducible using only data available at its timestamp (tested, not asserted) · Feature-target leak check on any new feature before it enters a model |
| **Hard rules** | Raw next-day return targets conflate market drift with stock selection — in a trending window every decile of a useless ranker shows positive "returns." All cross-sectional evaluation uses **market-excess** (and where relevant sector-excess) returns. |

### S3 · Alpha

| | |
|---|---|
| **Input** | S2 feature matrix |
| **Output** | Market regime score → CASH / trade decision; per-ticker score + confidence tier (HIGH/MID/LOW/NO_TRADE); event-risk level (LOW/MED/HIGH). **Every output record carries a data-lineage stamp** (timestamp of the S1 snapshot it was computed from) so S8 can distinguish model error from stale-input error |
| **Metrics** | **Regime gate**: % of gated (cash) days where universe median return was negative (gate precision); opportunity cost of gated days · **Scorer (the gate to go live, per §5)**: Spearman IC vs. excess returns on purged OOS ≥ 0.03 sustained; top-vs-bottom decile spread > 0 with p < 0.05 on **non-overlapping** windows; hit rate vs. 50% null · **Event veto**: % of vetoed names with realized |move| > 2× universe median on event day |
| **Hard rules** | Scorer is **disabled for live sizing** until §5 passes — currently informational-only in reports, labeled with its measured (lack of) skill. Deterministic facts (days-to-earnings) and LLM classifications must agree before either triggers real-money action alone. |

### S4 · Portfolio Construction

| | |
|---|---|
| **Input** | S3 confidence tiers + approved candidates; account net-liq; current positions |
| **Output** | Per-name dollar target → whole-share order spec `{ticker, qty, limit_price}` |
| **Metrics** | **Allocation deviation**: `|realized_$ − target_$| / target_$` per position, logged every sizing pass; reject if overshoot > 35% · Concentration: max single-name % (cap 25%) · Total deployed vs. budget (a whole-share floor once turned a $2,000 budget into $2,232 deployed, with the two lowest-conviction names as the largest positions) · Fee drag: commissions / gross P&L per day |
| **Hard rules** | **Dollar value is the unit of account; share count is a derived quantity.** Verify realized value against the per-position target after every sizing pass — the account-level cap alone cannot catch per-position inversion (hit twice: 2026-07-22, 2026-07-24). Undershoot (round-down) is acceptable; overshoot is not. |

### S5 · Execution

| | |
|---|---|
| **Input** | S4 order specs; IBKR Gateway session |
| **Output** | Verified fills; updated position state; trade log |
| **Metrics** | Fill rate within limit window · Slippage vs. limit price · Reconciliation mismatches between internal state and `ib.positions()` (target: zero; alert on any) · Order rejection count · API client-session count (Gateway degrades under many concurrent sessions — observed full connection wedge) |
| **Hard rules** | Limit orders only (0.3% offset RTH / 0.5% AH). Position sync against broker ground truth before every order. Fill status verified by polling, never assumed from submission. Per-ticker cooldown after any losing exit — no same-session re-entry. Internal JSON logs do not survive restarts; IBKR fills are the only cross-restart truth. |

### S6 · Intraday Risk Loop (15-min cycle)

| | |
|---|---|
| **Input** | Open positions, live prices, news/event signals |
| **Output** | Exit orders; circuit-breaker halt |
| **Metrics** | Stop adherence: realized loss at exit vs. −2% trigger (gap = slippage + cycle latency) · Time-stop yield: P&L of D7 exits vs. counterfactual hold-to-EOD (validated +$5.76 net on 30 real entries; keep measuring live) · Decision-gate accuracy vs. known outcomes (predecessor best: 83%) and veto rate · Circuit-breaker activations |
| **Hard rules** | Hard stop (−2%) is deterministic and **never** routed through the LLM gate. Soft-signal exits (sentiment/event) always are. Exit models with negative validation results stay disabled — a live-money bug gets a defensive disable, not a live patch under pressure. |

### S7 · Reporting

| | |
|---|---|
| **Input** | Fills, positions, daily analysis outputs |
| **Output** | Fill emails (HTML, verified status); morning report (regime, ranked list **with honest calibration note**, earnings flags with mismatch warnings); health alerts (escalating for critical, deduped for non-critical) |
| **Metrics** | Delivery success rate · Alert latency for critical failures · **Honesty check**: any displayed "prediction" must carry its measured historical hit rate and sample size — a near-coin-flip signal displayed as a confident price target is a defect (shipped once; removed) |
| **Hard rules** | Reports run every trading day regardless of regime — cost gating must never silently drop decision-relevant information (an earnings beat by a top-ranked name was once invisible because the check was skipped on a no-trade day). |

### S8 · Evaluation & Continuous Improvement

| | |
|---|---|
| **Input** | Trade logs, IBKR fills (ground truth), full-universe prediction records **with per-prediction data-lineage stamps**, S1 ingestion logs (fetch timestamps, coverage), S4 sizing logs (target vs. realized dollars), S5 execution logs (orders, fills, rejections), missed-opportunity scans |
| **Output** | Nightly retrospective (root cause → ≤1 fix proposal → backtest → gated deploy or DEFER); **nightly cross-stage audit report** (data-freshness lineage, sizing fidelity, execution quality — see below); refreshed rank calibration (monthly); prediction-quality time series |
| **Metrics** | **Daily IC**: Spearman(prediction, realized excess return) across the full universe — not just held names · Realized-vs-backtest gap per deployed change · Fix survival rate (proposed → validated → still-positive after 30 live days) · **Stale-input prediction rate**: % of the day's predictions whose lineage stamp shows input data older than the trading session (target 0; every stale prediction excluded from IC scoring and flagged) · **Sizing fidelity aggregate**: distribution of S4 allocation deviation across the day's orders (flag any position that breached tolerance or inverted the intended weight ordering) · **Execution quality aggregate**: fill rate, slippage vs. limit, rejections, reconciliation mismatches — from logs vs. IBKR fills, not from in-memory state · **Cost-per-trigger regressions** from the cost ledger (see cross-cutting metric): flag any trigger type > 2× its trailing-median cost |
| **Hard rules** | **Every prediction record must stamp the timestamp of the data snapshot it used — no lineage, no evaluation** (predictions made on placeholder/stale inputs are a data-pipeline defect, not model error, and must be attributed as such; the predecessor ran 22 consecutive regime cycles on a pre-market placeholder value with nothing detecting it). The nightly audit closes the loop on S1/S4/S5, not just S3 — a correct prediction sized wrongly or filled badly is still a system failure and must land in the retrospective with the right stage attribution. A fix with no historical data to test against isn't general enough — reformulate into a mechanically testable rule, up to 3 iterations; if none beat baseline, **DEFER is a valid outcome**. Never re-validate a fix on the same trades used to derive it. Same-day anecdote replay is not a backtest. |

---

### Cross-cutting metric (all stages): cost per trigger

Every pipeline invocation starts from a **trigger** — a cron entry firing, a
15-min review cycle, a morning-report run, a nightly retrospective, a manual
invocation. The metric is the **fully-loaded cost attributed to that trigger**,
counting every billable action it transitively initiates:

- **Trace ID propagation**: each trigger mints a `trigger_id`; every spawned
  action (LLM API call, web search, data fetch, broker order) carries it. If
  one trigger initiates two LLM calls inside earnings extraction, both calls'
  token costs land on that trigger — no orphan spend.
- **Cost ledger**: one append-only record per billable action:
  `{trigger_id, trigger_type, stage, provider, tokens_in, tokens_out,
  web_searches, unit_cost, commission, timestamp}`. Broker commissions
  attribute to the execution trigger that placed the order, so fee drag and
  API spend are readable in the same ledger.
- **Reported metrics**: cost per trigger instance · cost per trigger *type*
  per day (the actionable series) · cost per stage per day · day-over-day
  regression alert when a trigger type's cost jumps > 2× its trailing median.

Why this is a first-class metric and not an afterthought — all three of these
burned real money in the predecessor and were only found by manually reading a
billing dashboard after the fact:

1. A morning-report script ran its full compute twice per trigger (compute
   pass + render pass), silently doubling ~25 grounded LLM calls to ~50 every
   day. Cost-per-trigger would have shown the report trigger at 2× its
   expected cost from day one.
2. A health check invoked a full LLM CLI call every 15 minutes — 52 calls/day
   from a trigger whose job was a boolean auth probe.
3. A cost-*saving* gate then over-corrected and silently skipped
   decision-relevant earnings checks on no-trade days. Per-trigger cost
   visibility is what allows cutting real waste **without** blind gating that
   drops information — you cut the duplicated calls, not the informative ones.

S8's nightly audit consumes this ledger: cost-per-trigger-type regressions are
surfaced next to prediction quality and execution quality, and any proposed
"optimization" must show which trigger's cost it reduces and prove it drops no
decision-relevant output.

---

## 4. Repository layout (target)

```
stock-predictor/
├── docs/DESIGN.md            # this file
├── src/
│   ├── feature_store/        # registry + bitemporal storage + the one read API
│   ├── data/                 # S1 ingestion (prices, macro, earnings, LLM signals)
│   ├── signals/              # S2 derived features (point-in-time tested)
│   ├── alpha/                # S3 regime gate, scorer (gated), event risk
│   ├── portfolio/            # S4 sizing with value-vs-target verification
│   ├── execution/            # S5 IBKR adapter: orders, fills, sync, cooldown
│   ├── risk/                 # S6 stops, time-stop, circuit breaker, decision gate
│   ├── reporting/            # S7 emails, morning report, health checks
│   └── evaluation/           # S8 retrospective, calibration, IC tracking
├── tests/                    # unit tests; money-touching paths are mandatory
└── ops/                      # cron entries, runbooks
```

Never in git: credentials, account identifiers, market data, trade history,
model binaries (see `.gitignore`).

---

## 5. Validation protocol (the gate to live capital)

1. **Split by time** with real gaps implemented in code: train → **15-day
   purge** → validate → **7-day embargo** → test. (The predecessor *declared*
   these gaps in a manifest but never applied them to any date boundary.)
2. **Test window is touched once.** Hyperparameters and thresholds are chosen
   on validate only.
3. **Excess returns, not raw returns**, for all cross-sectional claims.
4. **Non-overlapping windows for significance.** Overlapping multi-day
   forward returns inflated an apparent p < 0.001 result to p ≈ 0.3 when
   corrected — that check is mandatory, not optional.
5. **Null baselines always reported**: predict-zero MSE, 50% hit rate,
   universe-average return. A model that doesn't beat the null doesn't ship.
6. **Multi-seed robustness** (≥5 seeds/perturbations, all positive) before
   any promotion.
7. **Staged go-live**: paper (dry-run, ≥5 sessions) → live with minimum size →
   full size. Any restart/config change requires explicit confirmation the
   new code is what's running.

## 6. Current status & migration order

| Layer | Status |
|---|---|
| Feature store + trigger/cost logging | **Migrated 2026-07-24** — `src/feature_store`, `src/common`; 11 passing contract tests; smoke-tested on live data |
| Data + earnings signals (S1) | **Migrated 2026-07-24** — `src/data` (prices, macro, calendar, grounded earnings extraction with per-call cost attribution); earnings extraction is observation-only |
| Execution/safety (S4–S6 core) | **Next to migrate** — proven in production, needs modularization + unit tests |
| Ops/reporting (S7) | Migrate after execution, with cost fixes retained |
| Alpha scorer (S3-A2) | **Not migrated as-is** — measured no-edge; rebuild around fundamentals/earnings features and pass §5 first |
| Regime gate (S3-A1) | Migrate with S3 scaffolding; it demonstrated correct cash calls live |
| Automated trading cron | **Off** until cutover criteria: all above migrated + tested + §5 pass for any live signal |
