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
Auto-generated from `performance.log`. Train **2026-06-09 → 2026-07-08** · dev **2026-07-10 → 2026-07-23** · 109 tickers · 25 features · label `end_of_day_forward_return(H=1d)`.

| model | type | key dev metric | vs baseline |
|---|---|---|---|
| `baseline_naive` | regression | price RMSE 10.335 | +0.00% vs naive |
| `baseline_mean` | regression | price RMSE 10.436 | -0.98% vs naive |
| `ridge` | regression | price RMSE 10.647 | -3.02% vs naive |
| `lasso` | regression | price RMSE 10.566 | -2.24% vs naive |
| `elasticnet` | regression | price RMSE 10.598 | -2.55% vs naive |
| `random_forest` | regression | price RMSE 10.356 | -0.20% vs naive |
| `extra_trees` | regression | price RMSE 10.395 | -0.58% vs naive |
| `histgbm` | regression | price RMSE 10.451 | -1.12% vs naive |
| `updown_clf` | classification | down precision 58% @ conf 0.65 | 5.1× base rate |
<!-- MODELS:END -->
