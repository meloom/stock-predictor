# h3 — 3-trading-day forward window

Predict whether a name moves **> +3% / < −3% over the next 3 session(s)**. Metric: per-day precision@k (top-k conviction picks/day; fraction that hit). 23217 labeled rows. Random-pick base rate: **up 20.3%, down 18.0%**. `ece` = calibration error of confident calls (lower = probabilities more trustworthy).

| model | up@1 | up@5 | down@1 | down@5 | ece |
|---|---|---|---|---|---|
| **`baseline_random`** | 20% (1.0×) | 20% (1.0×) | 18% (1.0×) | 18% (1.0×) | — |
| `logistic` | 26% (1.3×) | 26% (1.3×) | 35% (1.9×) | 30% (1.7×) | 37.2% |
| `histgbm` ⭐ | 31% (1.5×) | 26% (1.3×) | 35% (2.0×) | 29% (1.6×) | 39.0% |
