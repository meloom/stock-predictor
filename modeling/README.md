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
**Metric: PRECISION on big moves** (up if fwd > +3%, down if < -3%). Evaluated across **19 rolling 4wk-train/2wk-dev windows** (many regimes), pooling confident calls. RMSE dropped (it measured market timing, not selection).

**Baseline = random-guess precision = the base rate: up 9.6%, down 8.3%.** Beat these to have signal. (Up-precision is also shown restricted to UP-market dev windows, where up-moves aren't structurally rare — base 10.6%.)

| model | up precision | up precision (up-markets) | down precision | down lift vs base |
|---|---|---|---|---|
| **`baseline_random`** | 10% | 11% | 8% | 1.0× (base) |
| `logistic` | 25% (376) | 26% | 16% (544) | **1.9×** |
| `random_forest` | 23% (137) | 24% | 18% (44) | **2.2×** |
| `extra_trees` | 30% (37) | 33% | 16% (143) | **1.9×** |
| `histgbm` | 29% (65) | 26% | 41% (17) | **5.0×** |
| `gradient_boosting` | 32% (28) | 25% | 28% (18) | **3.3×** |
<!-- MODELS:END -->
