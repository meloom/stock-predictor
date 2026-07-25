# Predicting near-earnings stock moves — signal research

Motivation: our champion is earnings-blind — the top recall failures are all 20–31%
one-day earnings reactions (see `ERROR_ANALYSIS.md`). Predicting the earnings move
is a *different problem* from momentum/selection: it's an **expectations-and-options
game**, not a price/volume one. This is a market-research synthesis (literature +
20 real 2025–26 instances) of what signals actually help, and which are usable given
our data.

## 20 instances studied (date, 1-day move, real driver)

| # | ticker | date | move | driver — the actual reason (web-confirmed) |
|---|---|---|---|---|
| 1 | ZS | 2026-05-26 | −31% | beat rev/EPS but **CUT FCF-margin guidance** (26.5→22.8%) + sales-lead exits |
| 2 | TEAM | 2026-04-30 | +29% | **beat-and-raise** (EPS 1.75 vs 1.34, raised FY) |
| 3 | NET | 2026-05-07 | −23% | beat but **20% layoff** (restructuring shock) |
| 4 | MDB | 2026-05-28 | +20% | **beat-and-raise** (rev +25%, Atlas +29%) |
| 5 | MDB | 2025-12-01 | +22% | beat |
| 6 | NXPI | 2026-04-28 | +26% | beat + **strong Q2 guide** (+18% YoY) + AI |
| 7 | DDOG | 2025-11-05 | +23% | beat (rev +28%) + **raised Q4 forecast** |
| 8 | SNOW | 2026-05 (Q1) | +37% | **beat-and-raise** — AND was **down 50% into it** (low expectations) |
| 9 | FTNT | 2026-05-06 | +20% | beat / guide |
| 10 | INTC | 2026-04-23 | +24% | Q1 report beat |
| 11 | MRNA | 2026-05-07 | +12% | Q1 report |
| 12 | PYPL | 2026-02-03 | −16→−20% | **miss + weak 2026 outlook** (rev +3–4%, margin decline) + **CEO change** |
| 13 | NOW | Q2 2026 | −6.5%→reb | beat but **guidance quality** (Q2 upside was one-time federal timing) |
| 14 | GOOGL | Q2 2026 | −4.2% | beat but **capex surge / margin** worry |
| 15 | SAP | Q2 2026 | − | **miss** offset by cloud growth |
| 16 | SNOW | 2026-04-08 | −11.8% | pre-earnings de-rating (software selloff) |
| 17 | OKTA | 2026-04-08 | −11% | pre-earnings / sector |
| 18 | AMD | 2026-05-07 | +11% | report |
| 19 | RBLX | 2026-06-26 | +14% | report |
| 20 | ON | 2026-06-22 | −11% | report / guide |

**Pattern across all 20:** the reaction is about **guidance vs expectations, NOT the
headline beat.** Beat-and-raise → up (TEAM, MDB, NXPI, DDOG, SNOW). Beat-but-cut-guide
or structural-negative → down (ZS, NET, PYPL, NOW, GOOGL). **Low pre-earnings
expectations amplify up-moves** (SNOW down 50% → +37%); **"priced for perfection"
high multiples get punished even on a beat** (GOOGL). The headline EPS beat/miss alone
is nearly useless for direction.

## Signal taxonomy, ranked for a near-earnings model

**Tier 1 — expectations & options (the core):**
1. **Options-implied move** = ATM straddle × ~0.85 for the post-earnings expiry — the
   market's expected MAGNITUDE. Best magnitude predictor. Caveat: it **overprices** the
   realized move ~65–70% of the time (selling vol has an edge; IV crush punishes long
   options even when direction is right).
2. **Analyst estimate-revision momentum** — net up-minus-down revisions over 30–90d,
   *especially the last 2 weeks*. **Predicts the surprise DIRECTION better than the
   surprise itself** (the strongest ex-ante *directional* signal found).
3. **Estimate dispersion** (std of sell-side EPS) — high dispersion = genuine
   uncertainty; compare vs the implied move (rich/cheap event risk).
4. **Valuation / "priced for perfection"** — high forward multiple → asymmetric
   downside (a beat can still fall). We already have `fund.earnings_yield` etc.

**Tier 2 — positioning / crowding (predicts ASYMMETRY & squeezes):**
5. **Short interest** (>20% = crowded short) → asymmetric UP / squeeze risk on any
   positive. (NET/ZS-type "hated" names swing violently two-way.)
6. **Options skew / put-call ratio** — OTM-put skew = institutional downside hedging =
   asymmetric DOWN risk not visible in consensus EPS.
7. **Pre-earnings drift** — price/return momentum into the report; professionals
   position ahead, so the pre-announcement return weakly predicts the surprise
   direction (weaker/│reversed for retail-heavy names).

**Tier 3 — fundamentals & history:**
8. **Historical earnings-move size & surprise consistency (SUE)** — does the name
   habitually beat, and how big does it usually gap? Persistence is real.
9. **Quality of beat** — revenue-growth beat vs cost-cutting beat; guidance raise vs
   maintain.

**Tier 4 — text / NLP (the frontier, mostly POST-event):**
10. **Earnings-call transcript sentiment** (FinBERT/LLM tone, hedging language,
    guidance-change wording) — adds signal beyond the numbers, best for **PEAD** (the
    drift *after* the report), not the initial gap.

## Honest conclusions for our system

1. **The initial gap DIRECTION is close to unpredictable from pre-event public data.**
   It hinges on guidance vs unpublished "whisper" expectations. Even pros miss it (ZS
   beat and crashed −31%). Implied move predicts **magnitude**, not **direction**.
2. **Best PIT-safe, tractable targets for us:**
   - **Magnitude / "big move likely"** (recall/risk) → options-implied move + estimate
     dispersion + historical move size. This matches our `earnings_block` finding
     (helped magnitude recall +5.1pp, not direction) — it belongs in **Model 1** of the
     magnitude×direction split.
   - **Direction tilt (weak but real)** → analyst-revision momentum (last 2 wks) +
     short-interest asymmetry + pre-earnings drift. Use as a small tilt, abstain when
     conflicting.
   - **PEAD (day-after drift)** is *more predictable than the gap* and PIT-safe to trade
     the session AFTER the report (using the realized surprise + call tone), concentrated
     in less-followed names (~1.5–3% edge). **This may be a better target for us than the
     gap itself.**
3. **Why earnings prediction needs new data infrastructure:** the Tier-1/2 signals need
   an **options feed** (implied move, skew — not reliable in yfinance), **analyst
   estimate revisions & dispersion** (estimate data vendor), **short interest** (yfinance
   has some), and **transcripts** (NLP). This is exactly why the champion is blind: none
   of these are in our price/volume/fundamental feature set.

## Results — signals integrated + tested on our data (2026-07-25)

Built two PIT-safe blocks in `augment_features.py` (the yfinance-fetchable ones;
short interest & implied-move are current-snapshot only, not historically PIT):
- `analyst` — net upgrades−downgrades trailing 30d/90d + recent-downgrade flag
  (109/109 tickers, 3.8k upgrades / 3.5k downgrades, dated → PIT-safe).
- `preearn` — pre-earnings 5d/10d drift gated to names reporting within 10 sessions.

**Tested across ALL models over ALL rolling windows** (`eval_signals.py`), per-day
precision@1, baseline vs +signals:

| model | up@1 base→+sig | down@1 base→+sig |
|---|---|---|
| logistic | 24.2 → 22.1 (−2.1) | 16.8 → 15.8 (−1.1) |
| random_forest | 20.5 → 17.4 (−3.2) | 13.2 → 13.2 (0.0) |
| histgbm | 21.6 → 20.0 (−1.6) | 23.2 → 21.6 (−1.6) |
| gradient_boosting | 18.9 → 20.0 (+1.1) | 24.2 → 21.6 (−2.6) |
| **DUAL champion** | **24.2 → 22.1 (−2.1)** | **23.2 → 21.6 (−1.6)** |

**On the daily directional model the signals HURT (−1.5 to −3pp).** Reason (as the
research predicted): they matter only in the ~4% of rows near a report, so as
always-on features they are noise on the other 96% and dilute the momentum/
extension signal. **NOT promoted to `PREDICTOR_FEATURES`.**

**In their proper domain (near-earnings rows only, `days_to_earn ≤ 10`, n=2954) the
signals ARE real but weak:**
- analyst **upgrades → P(big-up) 13.3% vs 11.3% base (+2pp)**, P(big-down) 8.9% vs
  10.3% — a correct, modest bullish tilt.
- recent-downgrade flag → noisy/contrarian (downgraded names had *lower* P(big-down),
  likely already priced/oversold) — not a clean short signal.
- positive pre-earnings drift → weak upward continuation (+0.15% mean fwd); negative
  drift is two-way (no edge).

**Conclusion:** these belong in a **dedicated near-earnings module** (or Model 1 of
the magnitude×direction split), NOT the daily predictor. The blocks + fetch + eval
are kept in the codebase for that future module. This is the third confirmation
(after `earnings` and `insider`) that event signals don't help the daily directional
metric — the direction of an earnings move is genuinely close to unpredictable
ex-ante, exactly as the 20-instance study concluded.

## Proposed next steps (data-gated, priority order)
1. **Analyst-revision momentum** — cheapest ex-ante *directional* signal; yfinance
   `recommendations` / `upgrades_downgrades`, cached. (Backlog item #2.)
2. **Short interest** — yfinance `.info` shortPercentOfFloat; asymmetry/squeeze feature.
3. **Options-implied move + skew** — needs an options data source; best *magnitude*
   signal → Model 1. Evaluate a provider.
4. **PEAD target** — reframe: predict the day-AFTER drift (surprise sign + call tone)
   rather than the gap. Most tractable earnings edge.
5. **Transcript NLP** — FinBERT/LLM tone → guidance-change detection. Highest ceiling,
   highest cost; do last.

## Sources
Options implied move / straddle: [SpotGamma](https://spotgamma.com/free-tools/implied-earnings-moves/),
[PyQuant](https://www.pyquantnews.com/the-pyquant-newsletter/options-predict-stock-price-moves-after-earnings),
[ORATS](https://orats.com/university/volatility-around-earnings). IV crush:
[Option Alpha](https://optionalpha.com/learn/iv-crush), [CME Elite](https://www.cmelitegroup.com/knowledge-hub/how-earnings-move-stocks-expected-moves-iv-crush-and-overnight-gaps/).
Analyst revisions / SUE / PEAD: [Quantpedia PEAD](https://quantpedia.com/strategies/post-earnings-announcement-effect),
[Easton-Gao-Gao pre-earnings drift (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1786697),
[Alpha Suite PEAD](https://alpha-suite.org/blog/post-earnings-announcement-drift).
Positioning: [DeepVue short interest](https://deepvue.com/fundamentals/short-interest/).
Guidance>beat: [StockAlarm](https://pro.stockalarm.io/blog/earnings-guidance-vs-results),
[Kavout](https://www.kavout.com/market-lens/why-do-stocks-drop-even-after-beating-earnings).
NLP: [ACM AI-Finance PEAD+text](https://dl.acm.org/doi/10.1145/3604237.3626861).
Instances: [ZS](https://tickeron.com/blogs/zscaler-zs-shares-drop-31-after-strong-q3-revenue-beat-on-cautious-guidance-14051/),
[TEAM](https://seekingalpha.com/news/4582907), [NET](https://www.cnbc.com/2026/05/07/cloudflare-net-q1-2026-stock-earnings-layoffs.html),
[NXPI](https://www.tikr.com/blog/nxp-semiconductors-stock-surges-26-after-q1-2026-earnings-beat),
[DDOG](https://www.cnbc.com/2025/11/06/datadogs-ddog-stock-earnings.html),
[SNOW](https://www.fool.com/investing/2026/05/31/software-was-the-markets-big-laggard-this-year-sno/),
[PYPL](https://seekingalpha.com/news/4546116), [NOW/GOOGL](https://www.investing.com/news/transcripts/earnings-call-transcript-alphabet-beats-q2-2026-estimates-shares-fall-on-capex-surge-93CH-4807140).
