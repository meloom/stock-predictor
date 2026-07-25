# h2 — 2-trading-day forward window

Predict whether a name moves **> +3% / < −3% over the next 2 session(s)**. Metric: per-day precision@k (top-k conviction picks/day; fraction that hit). 23326 labeled rows. Random-pick base rate: **up 15.8%, down 14.1%**. `ece` = calibration error of confident calls (lower = probabilities more trustworthy).

| model | up@1 | up@5 | down@1 | down@5 | ece |
|---|---|---|---|---|---|
| **`baseline_random`** | 16% (1.0×) | 16% (1.0×) | 14% (1.0×) | 14% (1.0×) | — |
| `logistic` ⭐ | 28% (1.8×) | 24% (1.5×) | 31% (2.2×) | 27% (1.9×) | 41.5% |
| `histgbm` | 26% (1.6×) | 25% (1.6×) | 24% (1.7×) | 26% (1.8×) | 38.2% |
