# modeling/ — the experimentation zone

Train models with a standardized PIT-correct setup, measure honestly on ONE
decided metric, promote only measured winners into the live pipeline (`src/`).

**Metric (decided):** per-day **precision@k** on big moves — each day, take the
top-k highest-conviction longs and shorts; precision@k = the fraction that
actually moved > ±3%. The random baseline is the base rate (up 9.4% / down 8.1%).
RMSE and the old peak-precision columns are retired.

**Live model:** the **side-specific DUAL classifier** (`src/s3_predictors.py`
`train_dual_classifier` / `predict_proba_eod`) — **logistic for the long/up side,
HistGBM for the short/down side** (loop-2 finding: logistic reads up-moves better,
up@1 24% vs 22%; HistGBM reads crashes better, down@1 23% vs 17% — the dual beats
either single model on both sides). Reconciled from a prior train/serve mismatch
(production once ran a Ridge regression while diagnostics ran the classifier).
Champion feature set = the 25 S1/S2 features **+ the `xh.*` long-horizon extension
block**. Per-day precision@1 ≈ 2.6× (up) / 2.9× (down) the base rate. See
`LOOP2_FINDINGS.md` for the error-analysis loop that produced it.

### Prediction windows (horizons)
Models are organized by **prediction window** — how many trading days ahead the
label looks. Each `modeling/h{N}/README.md` holds the models tried for that window
(N ∈ {1,2,3,5,6,7}); the table below is the index across ALL windows. Features are
horizon-independent, so `multi_horizon.py` builds the panel once and just
relabels per window. Finding: the **edge (lift over the base rate) is strongest at
h1 and decays as the window lengthens** — longer windows have higher raw precision
only because a 3% move gets more common (higher base rate), not because the model
is more skilled. Choosing a hold length is therefore a *net-of-cost* tradeoff
(lower edge vs lower turnover), not a "more signal" decision.

### Files
- `harness.py` — standardized spine: `build_full_dataset` / `load_full_dataset`
  (one fetch → S2 across the year → panel, cached), `make_labels` (+1/0/−1 at
  ±3%), `rolling_windows` (4wk-train / 2wk-dev, purged), `prepare_window`, `fit`,
  `log_performance`, `promote`, `meets_bar`.
- `multi_horizon.py` — trains + evaluates the roster per prediction window, writes
  each `h{N}/README.md` and this index. Reports per-day precision@k + calibration
  error (ECE) — currently 32–43%, i.e. probabilities are badly overconfident and
  need calibration before the strategy consumes them.
- `backtest.py` — cost-aware long/short P&L (the honest "is it profitable" test).
- `augment_features.py` — error-analysis feature blocks (BLOCKS dict): `xhorizon`
  (**promoted**), `earnings` (validated for the magnitude head), and the tested-
  and-dropped `ext` / `sector` / `macro` / `insider`. Bars/macro/insider/earnings
  fetched once and cached under `runtime/`.
- `eval_augmented.py` — does a block lift per-day precision@k vs baseline?
- `eval_magnitude.py` — the RIGHT test for earnings: does it lift recall of big
  (esp. earnings-reaction) moves in a `P(|R|>3%)` magnitude model?
- `extract_errors.py` — mines the champion's top precision failures (confident-
  wrong) and recall failures (biggest missed moves) → `error_examples2.json`.
- `rebuild_readme.py` — regenerates the models table below (the auto block).
- `model_*.py` — one model per file (baselines + variants).
- **`ERROR_ANALYSIS.md`** — grounded root-cause write-up driving every feature.

Rule: no model reaches the pipeline except by beating the baseline on the decided
metric. Experiments are where you're allowed to fail; the live pipeline is where
only measured winners live.

## Models tried (auto-logged)

<!-- MODELS:START -->
**All models, all prediction windows.** Each row is the best model for a horizon (full roster in `modeling/h{H}/README.md`). Metric: per-day precision@1 (single best conviction pick/side per day) vs the horizon's random base rate. Longer windows have a higher base rate (more time to move 3%) — read the lift (×), not the raw %.

| window | best model | up@1 | down@1 | base up/down | ece |
|---|---|---|---|---|---|
| [h1](h1/README.md) — 1d | `histgbm` | 22% (2.3×) | 23% (2.9×) | 9%/8% | 42.9% |
| [h2](h2/README.md) — 2d | `logistic` | 28% (1.8×) | 31% (2.2×) | 16%/14% | 41.5% |
| [h3](h3/README.md) — 3d | `histgbm` | 31% (1.5×) | 35% (2.0×) | 20%/18% | 39.0% |
| [h5](h5/README.md) — 5d | `logistic` | 31% (1.1×) | 36% (1.6×) | 27%/23% | 33.4% |
| [h6](h6/README.md) — 6d | `histgbm` | 39% (1.4×) | 29% (1.2×) | 29%/25% | 36.1% |
| [h7](h7/README.md) — 7d | `logistic` | 33% (1.1×) | 44% (1.7×) | 30%/26% | 31.7% |
<!-- MODELS:END -->
