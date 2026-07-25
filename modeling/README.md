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
**Metric: PRECISION on big moves** (label up if next-day return > +3%, down if < -3%, else neutral). We only judge up/down calls the model is confident about; a neutral flagged up/down is the costly error. (RMSE was dropped — it measured market timing, not stock selection.)

Train **2026-06-09 → 2026-07-08** · dev **2026-07-10 → 2026-07-23** · 109 tickers · 25 features. Random-guess precision (base rate): **up 8.6%**, **down 11.5%** — beat these to have signal.

| model | up precision (calls) | down precision (calls) | down lift vs base |
|---|---|---|---|
| `logistic` | 14% (124 @conf 0.43) | 50% (12 @conf 0.75) | **4.4×** |
| `random_forest` | 12% (17 @conf 0.5) | 53% (15 @conf 0.48) | **4.7×** |
| `extra_trees` | 14% (14 @conf 0.45) | 56% (16 @conf 0.43) | **4.9×** |
| `histgbm` | 11% (79 @conf 0.35) | 58% (12 @conf 0.65) | **5.1×** |
| `gradient_boosting` | 13% (130 @conf 0.25) | 73% (11 @conf 0.63) | **6.3×** |
| `knn` | 17% (35 @conf 0.23) | 0% (12 @conf 0.2) | — |
| `mlp` | 14% (65 @conf 0.55) | 35% (31 @conf 0.8) | **3.1×** |
<!-- MODELS:END -->
