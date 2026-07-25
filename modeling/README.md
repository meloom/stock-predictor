# modeling/ — the experimentation zone

Train different models with a proper purged train/val/test setup, measure
honestly, iterate until one is good enough — THEN promote it to `models/`.

- `harness.py`  — shared spine: `prepare_panel` (real PIT-correct panel via the
  live S1/S2 code — no train/serve skew), `purged_split` (15d purge / 7d
  embargo by date), `fit` (any sklearn estimator), `evaluate_all` (return-IC +
  price-vs-naive), `promote` (write artifact+metadata to models/), `meets_bar`
  (the promotion gate).
- `expNN_*.py`  — one experiment per file; each tries model(s), prints honest
  metrics, and promotes the best only with `--promote` AND only if it clears
  the bar.

Rule: no model reaches the pipeline except by being promoted through the
registry. Experiments are where you're allowed to fail; the registry is where
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
