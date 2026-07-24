---
name: modeling-protocol
description: The non-negotiable rules for training any model in this project. Load before writing or changing anything under modeling/ or models/, or before training/promoting a model.
---

# Modeling protocol

Standing rules for `modeling/` (experimentation) and `models/` (registry).
These are owner-mandated; do not deviate without explicit direction.

## Training / evaluation split — fixed
- **Training window = 4 weeks (20 trading days).**
- **Dev/eval window = the 2 weeks (10 trading days) immediately after training.**
- A **purge gap = the label horizon** sits between train and dev, so training
  labels (which look `horizon` days ahead) cannot overlap the dev window.
- `harness.prepare_window()` enforces this with an assertion — never hand-roll
  a different split.

## Universe — always all of it
- Train on the **full tracked universe** (`src/universe.py::UNIVERSE`), never a
  hand-picked subset. Changing coverage means editing `universe.py`, one place.

## Label strategy
- Default: **end-of-day forward return** (predict the close `H` trading days
  ahead; `H=1` is the literal EOD/next-day price predictor). Recorded in
  metadata as `label_strategy`.

## One model per file
- Each model gets its own `modeling/model_<name>.py`. No file trains multiple
  models. Add a model = add a file.

## Shared performance log
- Every training run appends one record to **`modeling/performance.log`**
  (append-only, JSON lines) via `harness.log_performance()`.
- Each record MUST include: model, label strategy, **training-data range**,
  **dev-data range**, **tickers**, **features**, dev metrics (IC, beats-null,
  price MAPE vs naive), and promotion outcome.

## Honesty gates (never skip)
- Report the **cross-sectional IC** (rank corr vs realized return, beats the
  predict-the-mean null) AND the **price error vs the naive persistence
  baseline** (tomorrow=today). A low MAPE that doesn't beat naive is a random
  walk in disguise — not skill.
- Missing features are mean-imputed, never sentineled.

## Promotion → the registry
- A model reaches `models/` only via `harness.promote()` and only if
  `harness.meets_bar()` clears (real held-out IC, beats null, beats naive).
- **Not clearing the bar is a valid, honest outcome** — the registry holds
  measured winners, not attempts.
- Promoted metadata (`models/<id>/metadata.json`, committed) carries: train
  range, dev range, tickers, features, label strategy, test metrics. The
  artifact (`artifact.pkl`) is gitignored and rebuilt from the model file.
- The pipeline loads models ONLY through `models/wrapper.py::Predictor.load()`
  — never trains inline.

## Registry ≠ live gate
- Being promoted/registered means "best measured candidate," NOT cleared for
  real capital. The §5 validation gate (non-overlapping-window significance,
  etc.) is a separate, stricter bar before any live trading.
