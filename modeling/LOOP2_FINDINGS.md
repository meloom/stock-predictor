# Improvement loop #2 — worst-period error analysis (10 iterations)

Each iteration: pick a bad dev window, mine the model's confident-wrong calls and
biggest missed moves in it, ground *why* those names moved (external analysis),
turn the missing information into a point-in-time-safe feature, test it under the
decided metric (per-day precision@k, walk-forward — [[feedback_eval_no_leakage]]),
and log the result here + in `performance.log` **whether or not it helps** (a
negative is a result, not a failure — we don't manufacture winners).

Champion at loop start: HistGBM, 31 features incl. `xh.*`; down-side per-day
precision@1 ≈ 2.9× the base rate.

| # | worst period | what was missed | hypothesis / feature | metric Δ | valid? |
|---|---|---|---|---|---|
| 1 | 2026-05-06..19 (−11.2%, Q1 earnings cluster) | earnings-driven moves (NET −24%, FTNT +20%, AMD/ON/MDB +11%) + confident bets run over by reports (QCOM, ON, ZS) | abstain from daily picks when name reports ≤1–3d away (strategy gate, not a model input) | up@1 −0.5pp, down@1 −0.5pp | **NO** — near-earnings = only 3–4% of rows, rarely the top pick; filter barely changes picks and drops earnings moves that went the right way |
| 2 | #2 2026-06-18..07-02 (−6.9%, semis-ATH crash), #3 2026-04-08..21 (−6.6%, April whipsaw) | all 3 worst windows are high-vol regimes with ±10%+ two-way swings; confident directional calls get run over | vol-regime conditioning: abstain (or down-weight) on high-VIX / high-dispersion days | VIX-abstain +1.0pp (up +1.8/down +0.2); dispersion-abstain −0.8pp | **NO / inconclusive** — precision is NON-monotonic in vol (mid best, low-VIX up@1 worst at 17%); worst windows are TAIL-event driven (a few big confident-wrong calls), not a gate-able regime |
| 3 | (same worst windows) many missed moves are consistent-direction; test if multi-horizon consensus is a better filter | select daily top-1 by cross-horizon agreement: mean / min of p across h1,h2,h3 | mean up+0.5/down−3.7pp; min(agree) up−1.1/down−0.5pp | **NO** — h1 model is already the best predictor of the next-day outcome; h2/h3 trained on different targets dilute it |
| 4 | #3 confident DOWN calls bounced (ORCL +5%, TTD +7%); #2 confident UP calls were semis at ATH that crashed — mean-reversion at extremes | RSI guard on picks: don't short oversold (RSI low), don't buy overbought (RSI high) | best case down +0.5pp, up −1.1pp | **NO** — `rsi14` is ALREADY a model input; a post-hoc gate on a feature the model uses adds no info, only drops picks. (Pattern: iters 1–4 all re-gate signals the model already has.) |
| 5 | logistic vs histgbm per side (from the confident-wrong split): logistic reads up-moves better, histgbm reads crashes better | **DUAL model: logistic for longs + histgbm for shorts** | up@1 **+2.6pp** (24.2 vs 21.6), up@5 +1.8pp, down held at 23.2% | **YES ✅ — PROMOTED** to live S3 (`train_dual_classifier`). Robust across K and consistent with logistic winning the up side across horizons. First win: came from a MODELING change, not re-gating features. |
| 6 | logistic (up-model) is linear → outlier-sensitive; try robust feature transforms | winsorize \|z\|≤3 + cross-sectional demean for the up-model | up@1 24.2 (unchanged), up@5 +1.0pp | **NO** — no lift at the top-1 operating point; marginal up@5 gain not worth complexity |

## Conclusion (after 6 iterations)

**One real win: the side-specific DUAL model (iter 5, promoted) — up@1 +2.6pp.**

**The decisive meta-lesson (iters 1–4, 6):** every *feature-gating* hypothesis
failed, and for the same reason — I kept re-gating signals the model **already
uses** (RSI, realized vol, extension). A post-hoc filter on an existing feature
adds no information; it only drops picks. The one thing that worked (iter 5) was a
**modeling** change (right learner per side), not a new gate.

**Why the worst windows stay bad:** they are dominated by *tail events* —
earnings surprises, sector-macro crashes, whipsaws — driven by information that is
**not in price/volume/fundamental features at all** (guidance, news catalysts,
rates shocks). No re-gating of existing features can recover them.

**So the remaining levers are NOT more of this loop.** They are:
1. **New information sources** — real-time news/NLP, analyst revisions, options-
   implied moves. This is the documented backlog ([[project_feature_backlog]]);
   `earnings` proved these help *recall/magnitude*, not direction.
2. **The two-model split + calibration** — ECE is 32–43% (probabilities badly
   overconfident); calibrated confidence is what a sizing strategy needs.
3. **Lower turnover** — the P&L is bled to costs, not to bad signal (see
   `backtest.py`).

Iterations 7–10 were deliberately NOT run as more feature-gate variants: doing so
would be selection-bias p-hacking (forbidden by [[feedback_eval_no_leakage]]).
The honest next step is a *new-information* feature (analyst/news) or the
two-model split — each a distinct, pre-specified experiment, not knob-tuning.
