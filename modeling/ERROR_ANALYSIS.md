# Error analysis — grounded root causes of the top confident-wrong calls

The improvement loop is driven by understanding **why** the champion model's
highest-conviction calls were wrong on specific dates, using external analysis of
each move, then engineering a point-in-time-safe feature that captures the driver.

**Leakage rule (non-negotiable):** we never feed the *reason a stock moved* as a
feature — that reason is only known after the move, so using it is the label
leaking back in. We only add signals that were **observable at the close of the
prediction day**. External analysis tells us *what category* of ex-ante signal we
lack; the feature must be computable before the move.

## Top confident-wrong UP calls (predicted up, actually dropped >3%)

### MRVL 2026-06-30 (conf 0.96, actual −8.7%)
External analysis (owner-provided Gemini breakdown + web search):
- **S&P 500 inclusion unwinding** — MRVL was added to the S&P 500 in late June;
  once the index-rebalance passive buying finished, demand softened → "sell the
  news."
- **Rising Treasury yields / macro de-risking** — a strong May jobs report spiked
  the 10-year yield (toward ~3.5%); long-duration, high-multiple AI/optical semis
  fell hardest.
- **Stretched valuation after a parabolic run** — +300% YTD, all-time high ~$329
  earlier in June → profit-taking.
- **CFO departure + insider selling** — CFO Willem Meintjes stepping down amid
  insider divestments.

### AMAT 2026-06-30 (conf 0.90, actual −10.0%)
- **All-time intraday high $739.67 on 06-30** — the model bought the exact top;
  ~14% correction followed.
- **Heavy insider selling** — CEO Gary Dickerson sold 20,000 sh (~$14.7M) on 06-30,
  after a mid-June wave (~$42.5M CEO + ~$14.4M CTO). Read as "valuation stretched."
- **Global semiconductor sell-off** — KOSPI plunge led by SK Hynix/Samsung; AI
  equipment rally seen as overbought.
- **Analyst repositioning** — Morgan Stanley preferred LRCX over AMAT (downgrade).
- **FCF squeeze** — record revenue but FCF contracted YoY (working capital + $500M
  Singapore expansion).
- **US-China export-control headwinds.**

### MU 2026-07-01 (top-1 long, actual −5.5%)
- **Memory sell-off / oversupply fears** — China CXMT's $8.55B IPO, CoreWeave
  hedging memory costs; stock fell ~27–29% from its ~$1,200 June high **despite**
  record DRAM pricing. Classic high-valuation profit-take.

### Common structure
Every top up-failure is: **a name at/near a 52-week high after a multi-month
parabolic run, reversing on a macro/rates + thematic de-risking day.** The 06-30
crash was one correlated sector event, not four independent misses.

## Driver → feature mapping

| Driver | Recurs in | Feature (PIT-safe) | Status |
|---|---|---|---|
| At/near 52-wk high after multi-month run | MRVL, AMAT, MU | `xhorizon`: ret_21/63/126d, dist-from-252d-high, new-high flag, above-high streak | **BUILT — CHAMPION (+1.3pp)** |
| Rising 10Y yields / risk-off de-rating high-multiple tech | whole cluster | `macro`: yield level+5d chg, VIX level+5d chg, yield_chg×run-up interaction | built — **dropped** (day-level, no x-sec lift) |
| Insider selling (CEO/CTO/CFO) | AMAT, MRVL | `insider`: trailing sale counts (90d/30d) + C-suite-sold flag (yfinance insider_transactions) | built + tested — **dropped**: real driver but REDUNDANT with `xhorizon` (insiders sell into the same parabolic-run-to-highs setups; −0.8pp on top of champion) |
| Analyst downgrade / repositioning | AMAT | recent rating-change flag | TODO — needs recommendations fetch |
| S&P 500 inclusion unwind | MRVL | days-since-index-add (curated calendar) | TODO — curated table |
| 20-day overextension (per-name) | — | `ext` block | tested: net-neutral, not promoted |
| Per-name sector momentum | — | `sector` block | tested: **hurt**, dropped |

## Results log — metric = per-day precision@k (pick top-k names/day, % that moved >±3%)

Baseline (25 features), across 19 rolling 4wk/2wk windows. Random daily pick =
base rate: **up 9.4%, down 8.1%**.

| feature set | up@1 | down@1 | mean Δ up | mean Δ down | verdict |
|---|---|---|---|---|---|
| baseline 25f | 21.1% (2.5×) | 20.0% (2.5×) | — | — | reference |
| **+ `xhorizon`** | 21.6% | **23.2% (2.9×)** | −0.1pp | **+2.7pp** | **CHAMPION (+1.3pp combined)** |
| + `ext` | 23.2% | 20.5% | +0.9pp | +0.6pp | marginal, not promoted |
| + `macro` | 16.3% | 21.1% | −2.5pp | −0.1pp | dropped |
| + `sector` | 16.8% | 16.8% | −2.1pp | −1.9pp | dropped (hurt) |
| + `xhorizon`+`macro`+`ext` | 21.6% | 20.5% | +0.2pp | +1.3pp | worse than xhorizon alone |

**Champion feature set = 25 baseline + `xhorizon` (6 features).** The long-horizon
extension features lift the DOWN side most (which stretched name will actually
crash), matching the error cluster's story. Macro/sector are market-level and
don't improve cross-sectional name selection — dropped honestly. Full per-run
records in `modeling/performance.log`.
