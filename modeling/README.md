# modeling/ — the experimentation zone

Train models with a standardized PIT-correct setup, measure honestly on ONE
decided metric, promote only measured winners into the live pipeline (`src/`).

**Metric (decided):** per-day **precision@k** on big moves — each day, take the
top-k highest-conviction longs and shorts; precision@k = the fraction that
actually moved > ±3%. The random baseline is the base rate (up 9.4% / down 8.1%).
RMSE and the old peak-precision columns are retired.

**Live model:** the 3-class big-move classifier (`src/s3_predictors.py`
`train_classifier` / `predict_proba_eod`) — reconciled from a prior train/serve
mismatch (production once ran a Ridge regression while all diagnostics ran this
HistGBM). Champion feature set = the 25 S1/S2 features **+ the `xh.*` long-horizon
extension block** (down-side precision@1 ≈ 2.9–3.1× the base rate).

### Files
- `harness.py` — standardized spine: `build_full_dataset` / `load_full_dataset`
  (one fetch → S2 across the year → panel, cached), `make_labels` (+1/0/−1 at
  ±3%), `rolling_windows` (4wk-train / 2wk-dev, purged), `prepare_window`, `fit`,
  `log_performance`, `promote`, `meets_bar`.
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
**Metric: per-day precision@k.** Each trading day, rank the model's confidence and take the top-k names (k highest-conviction longs, and separately k shorts); precision@k = the fraction of those daily picks that actually moved **> +3%** (up) / **< −3%** (down). This is the realistic trading read — a handful of best ideas per day. RMSE and the old peak-precision columns are retired.

Evaluated across **19 rolling 4wk-train / 2wk-dev windows**, **20 features** (incl. the champion `xh.*` long-horizon block). **Baseline = random daily pick = the base rate: up 9.4%, down 8.1%** — beat these to have signal. Cells show precision (lift × the base rate).

| model | up@1 | up@5 | down@1 | down@5 |
|---|---|---|---|---|
| **`baseline_random`** | 9% (1.0×) | 9% (1.0×) | 8% (1.0×) | 8% (1.0×) |
| `logistic` | 24% (2.6×) | 20% (2.2×) | 17% (2.1×) | 16% (2.0×) |
| `random_forest` | 21% (2.2×) | 19% (2.0×) | 15% (1.9×) | 14% (1.8×) |
| `extra_trees` | 18% (1.9×) | 18% (2.0×) | 19% (2.4×) | 17% (2.1×) |
| `histgbm` | 22% (2.3×) | 19% (2.0×) | 23% (2.9×) | 18% (2.3×) |
| `gradient_boosting` | 18% (2.0×) | 21% (2.2×) | 25% (3.1×) | 20% (2.4×) |
<!-- MODELS:END -->
