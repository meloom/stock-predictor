# models/ — the model registry

Promoted models only. A model lands here from `modeling/` **when it clears the
promotion bar** (`harness.meets_bar`): real held-out IC, beats the null, and
beats the naive persistence baseline. A model that doesn't clear the bar is not
promoted — that's a valid, honest outcome, not a failure to try harder.

Per model: `models/<model_id>/`
- `metadata.json`  — committed: model type, features, horizon, purged split,
  held-out `test_metrics`, the experiment that produced it, timestamp.
- `artifact.pkl`   — gitignored: the fitted model + standardization stats.
  Rebuild by re-running the experiment in `metadata.experiment`.

`registry.json` indexes what's promoted. The pipeline loads a model only
through `models/wrapper.py::Predictor.load(model_id)` — never by training
inline. Swap the promoted model, the pipeline code doesn't change.
