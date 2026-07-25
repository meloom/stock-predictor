# Improvement loop — findings (10 iterations, 30 experiments)

> **CORRECTION (2026-07-25): the conclusion below ("random walk, nothing beats
> naive") was WRONG — a measurement artifact, not a market truth.** Deep-dive
> root cause:
> 1. **Wrong metric.** We judged a stock *selector* by absolute price/return
>    RMSE, which measures market *timing*. The train window was UP
>    (+0.22%/day) and the dev window was DOWN (−0.24%/day, 44% up-days), so
>    every model that learned the training drift predicted slightly positive
>    and lost to naive (predict 0). That's a train/dev regime flip, not a
>    stock-picking failure. The right target is cross-sectional EXCESS return
>    (demean per day); the right metric is cross-sectional IC + long-short
>    spread — both immune to market direction.
> 2. **Dev window far too small.** 2 weeks = 10 days. Daily cross-sectional
>    IC swings ±0.4 day to day, so a real ~0.03–0.06 IC is unmeasurable over
>    10 days — the mean is pure noise. (The very first predictor test, 2y /
>    20d horizon / ~400 test rows, DID show IC +0.065 — see
>    examples/s3_predictors.output.json. The tiny-window loop couldn't see it.)
> 3. **Broken data.** 6 of 25 features were 100% NaN (shares_outstanding +
>    analyst-EPS fetch failed during the cache build → market_cap,
>    book_to_price, earnings_yield, fcf_yield all empty).
>
> Fix: predict cross-sectional excess return; evaluate by IC/long-short over
> MANY rolling 4wk/2wk windows (accumulate dev days for statistical power);
> repair the shares/analyst fetch. The original 30-experiment table below is
> left as-is for the record — read it as "measured under the wrong metric on
> an underpowered, regime-flipped window", NOT as evidence of no signal.

---


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
