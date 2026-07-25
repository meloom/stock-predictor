# h5 — 5-trading-day forward window

Predict whether a name moves **> +3% / < −3% over the next 5 session(s)**. Metric: per-day precision@k (top-k conviction picks/day; fraction that hit). 22999 labeled rows. Random-pick base rate: **up 26.6%, down 22.9%**. `ece` = calibration error of confident calls (lower = probabilities more trustworthy).

| model | up@1 | up@5 | down@1 | down@5 | ece |
|---|---|---|---|---|---|
| **`baseline_random`** | 27% (1.0×) | 27% (1.0×) | 23% (1.0×) | 23% (1.0×) | — |
| `logistic` ⭐ | 31% (1.1×) | 32% (1.2×) | 36% (1.6×) | 35% (1.5×) | 33.4% |
| `histgbm` | 36% (1.4×) | 33% (1.2×) | 26% (1.1×) | 29% (1.3×) | 35.7% |
