# Alpha Report — how the best desks build one, and ours

A deep dive into how professional shops structure a single-name alpha report, the union of
their components, and a concrete mapping to **what we already have** vs **what's missing
and collectible**. The report we build (`/alpha?ticker=`) assembles our real S1/S2/S3/S4
signals into this structure.

---

## 1. Research — 8 professional frameworks and their components

**1. Sell-side equity research** (Goldman, Morgan Stanley, JPMorgan single-stock notes).
Components: rating (Buy/Hold/Sell), **12-month price target**, 3-bullet thesis, **catalyst
calendar**, valuation (P/E, EV/EBITDA vs peers & own history), **earnings estimates &
revisions**, key risks, **bull/base/bear scenario targets**.

**2. Barra / MSCI & Axioma factor risk models.** Components: **factor exposures** (value,
momentum, size, volatility, quality, growth, liquidity, yield), **predicted beta**,
factor vs **specific (idiosyncratic) risk** decomposition, factor-implied expected return,
stress/scenario VaR. The report is a *risk* lens: how much of the name is factor vs alpha.

**3. AQR / academic factor investing.** Components: the canonical premia — **Value,
Momentum, Quality (profitability/safety), Defensive/Low-vol, Size, Carry** — each as a
cross-sectional z-score/percentile, plus the factor's own recent performance and the
name's loading on it. Emphasis: cross-sectional ranking, not level.

**4. WorldQuant "101 Formulaic Alphas" / stat-arb signal cards.** Components per signal:
formula/definition, **IC (information coefficient) & rank-IC**, **turnover**, **decay /
half-life**, signal **Sharpe**, correlation to existing signals, **capacity**, drawdowns.
The unit is a *signal*, scored for standalone and marginal value.

**5. Multi-manager pod memos** (Millennium / Citadel / Point72). Components: thesis +
**variant perception** (why the market is wrong), **expected value from probability-weighted
scenarios**, **catalyst path & timing**, **risk/reward ratio**, **factor/beta-neutral
position sizing** (vol-target or Kelly fraction), **crowding/positioning**, explicit
**invalidation triggers** and monitoring KPIs.

**6. Morningstar-style fundamental.** Components: **economic moat**, **fair value estimate**,
**uncertainty rating** (→ margin of safety), capital allocation, bull/bear.

**7. Two Sigma / systematic ensemble.** Components: an **ensemble of signals with weights**,
combined score + per-signal & combined IC, **regime conditioning** (which signals work
now), an **execution-cost model**, capacity/liquidity limits.

**8. Event-driven / catalyst desks.** Components: **catalyst calendar**, the **options-implied
expected move**, **historical event reaction** (avg move/drift around earnings), positioning
into the event, post-event drift.

---

## 2. The complete component checklist (union) → our coverage

Legend: ✅ have · 🟡 partial · ❌ missing (collectible — see §3).

| # | Component | Source in a pro report | Ours |
|---|-----------|------------------------|------|
| A | **Identity & context** — price (live), sector/industry, market regime | all | ✅ price/macro/regime · ❌ **sector** |
| B | **The view** — direction, conviction, horizon(s) | pod / sell-side | 🟡 S4 event-risk + regime gate; per-ticker prediction pending deploy |
| C | **Predictor outputs** — per-horizon calibrated P(up)/P(down), **precision@k**, expected return, confidence | quant / systematic | 🟡 recorded skill (1d/3d/5d/7d, down ≈2–3× base); per-ticker live output pending |
| D | **Factor decomposition** — Value, Quality, Momentum, Size, Low-vol, Growth (z / percentile) | AQR / Barra | ✅ fund.* + xsec ranks + tech + xh · 🟡 growth (rev-YoY), factor betas |
| E | **Catalysts & events** — next earnings & days-to, analyst revisions, insider activity, options implied move | sell-side / event | ✅ earnings cal, revisions, insider, implied move · ❌ **index/corp-action events** |
| F | **Valuation** — earnings/fcf/book yields, margins vs **peers** & vs **own history** | sell-side / fundamental | ✅ ratios + xsec vs peers · 🟡 vs-history series · ❌ EV/EBITDA needs sector peers |
| G | **Risk & exposures** — predicted **beta**, realized vol, event risk, sector/factor exposure, short crowding | Barra / pod | ✅ vol, event-risk, short crowding · ❌ **predicted beta** (computable), factor betas |
| H | **Positioning / crowding** — short interest, insider flow, **institutional ownership (13F)**, analyst dispersion | pod / event | ✅ short, insider · 🟡 analyst count · ❌ **13F ownership**, target dispersion |
| I | **Narrative / NLP** — earnings-call & filing sentiment, guidance, surprise-vs-narrative | fundamental / modern-quant | 🟡 filings + XBRL collected; **NLP layer not built**; transcripts paid |
| J | **Scenario / expected value** — bull/base/bear with probabilities, **risk/reward** | pod / sell-side | 🟡 derivable once C is deployed (pred ± CI → scenarios) |
| K | **Recommendation & sizing** — composite alpha score, rank, action, **vol-target weight**, entry/exit, invalidation | pod / systematic | 🟡 compose from D+C+G; sizing rule to add |
| L | **Backtest / efficacy** — IC / hit / **precision@k**, decay, drawdown of the driving signals | quant | ✅ `/predictors` recorded per-horizon precision@k |
| M | **Costs & capacity** — liquidity (**ADV**), spread, turnover, capacity | systematic | ✅ ADV (from volume) · ❌ **bid/ask spread** |
| N | **Data quality** — coverage, freshness, PIT/as-of | all | ✅ `/data-collection` + as-of everywhere |

---

## 3. Gaps — what's missing, and whether we can collect it

**Collectible now (free), high value:**
1. **Sector / industry classification (GICS/SIC).** Needed for sector-neutral factors, peer
   sets, and valuation-vs-peers. Source: SEC company facts (SIC) or Polygon `/v3/reference/
   tickers/{T}` (sector). **Free.** → add to S1.
2. **Predicted beta & factor betas.** NOT missing data — missing *computation*. We have bars
   + SPY; regress the name's returns on SPY (beta) and on factor-mimicking portfolios. → add
   to S2. **No new collection.**
3. **Institutional ownership / 13F.** Source: SEC EDGAR 13F filings (free) — parse holdings
   by manager. Adds crowding/ownership. → extend the EDGAR collector.
4. **ADV / liquidity** — already derivable from `bars.volume` (dollar-ADV). → add to S2.

**Collectible, more effort / paid:**
5. **Bid/ask spread & depth** (transaction-cost model). Polygon quotes (paid tier) or IBKR.
6. **Analyst estimate dispersion & individual targets** (we have mean + count only). Paid
   (Refinitiv/FactSet/Zacks) or scrape.
7. **Index membership & corporate actions** (S&P add/drop, splits/spinoffs). Paid or scrape.
8. **Earnings-call transcripts** (already flagged): paid API for the forward-guidance NLP.

**Build, not collect (data is here):**
9. **S2 NLP layer** over `sec_filings` / `xbrl` (guidance tone, surprise-vs-narrative).
10. **Per-ticker predictor deployment** (component C): deploy the recorded 1d/3d/5d/7d
    champions to emit calibrated P(up)/P(down)/precision@k per name.
11. **Composite alpha score + vol-target sizing** (component K).

---

## 4. Our alpha report structure (`/alpha?ticker=`)

Assembled from real signals; sections with pending inputs are labeled, not faked.

1. **Header** — ticker, live price, sector*, as-of, market regime (VIX + breadth), overall
   **alpha score** + action.
2. **Prediction** — per-horizon direction (from `/predictors` skill) + event-risk gate;
   per-ticker calibrated output when C is deployed.
3. **Factor decomposition** — Value / Quality / Momentum / Size / Low-vol, each a percentile
   vs the universe (xsec ranks) + the raw ratio.
4. **Catalysts** — days-to-earnings, next date, recent analyst-revision drift, insider net
   flow, options-implied move.
5. **Valuation** — earnings/fcf/book yields, ROE, margins, vs-peer percentiles.
6. **Risk** — realized vol, event risk, short crowding, regime sensitivity, predicted beta*.
7. **Positioning** — short interest, insider flow, analyst consensus/target.
8. **Efficacy** — the recorded precision@k of the signals driving this name.
9. **Data freshness** — as-of per input.

`*` = flagged gap (sector, predicted beta) — collectible per §3.
