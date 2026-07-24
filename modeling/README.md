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
