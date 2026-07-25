# Improvement loop — findings (10 iterations, 30 experiments)

**Setup:** 4-week train / 2-week dev, full universe (109 tickers), 25 features
(technical + 7 lagged daily returns + fundamentals + cross-sectional ranks),
end-of-day (H=1) forward-return label. Cached panel (one fetch), champion =
lowest dev price-RMSE; naive persistence is the initial champion.

## Result: nothing beat the naive baseline

Across **30 combinations** of features × models, **not one beat naive
persistence** (predict tomorrow's price = today's price). The champion stayed
`baseline_naive` the entire loop.

| Best experiments (of 30) | price RMSE | vs naive | direction hit |
|---|---|---|---|
| naive baseline | 10.335 | — | — |
| interactions + RandomForest | 10.347 | −0.12% | 45.0% |
| raw + RandomForest | 10.356 | −0.20% | 45.0% |
| fundamentals-only + Ridge | 10.384 | −0.48% | 45.1% |
| squares + HistGBM | 10.385 | −0.49% | 46.8% |

Every model was 0.1–0.7% **worse** than naive on price RMSE, and every
direction hit-rate was **below 50%** (44–47%) — worse than a coin flip.

## What was tried

- **Features:** raw, pairwise interactions, squares, PCA(10), cross-sectional
  ranks, momentum/lag-only subset, fundamentals-only subset, winsorized.
- **Models:** Ridge (α=1/10/100), Lasso, ElasticNet, RandomForest, ExtraTrees,
  HistGradientBoosting (depths 2–3), KNN, MLP.

## Root cause (same every iteration)

The dev worst-errors are consistently **big real moves the model flattens
toward ~0**: 71 of 1090 dev observations moved >5% in a day — mostly
single-name earnings/news jumps that are **not predictable from price/volume/
fundamental features**. The predictable part of a 1-day move is tiny and
dominated by noise, so:
- **Feature POV:** none of our features carry next-day information; the
  signal that would matter (imminent news/earnings surprises) isn't in them.
- **ML POV:** with a near-zero signal-to-noise target, every model correctly
  learns to predict ≈0 return (≈ naive), and any deviation it makes is noise
  that *adds* error — hence uniformly worse-than-naive.

## Honest conclusion

**A 1-day (end-of-day) price predictor is a random walk** for this feature
set — a well-established result, now demonstrated across 30 real experiments
under a fixed, purged protocol. More features/models won't fix it at this
horizon. The two directions with a real chance (per earlier measurement):
1. **Longer horizon** (weekly/monthly) — earlier runs showed a small but
   positive cross-sectional signal (~0.06 IC, +2% price-RMSE vs naive at 20d).
2. **A different information source** — the one feature family with evidence
   (earnings guidance/surprise) needs historical fundamentals to contribute,
   which are ~flat over a 4-week window.

The loop worked exactly as intended: it didn't manufacture a winner. It
measured 30 methods honestly and kept the baseline because nothing beat it.
