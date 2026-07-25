# h1 — 1-trading-day forward window

Predict whether a name moves **> +3% / < −3% over the next 1 session(s)**. Metric: per-day precision@k (top-k conviction picks/day; fraction that hit). 23326 labeled rows. Random-pick base rate: **up 9.4%, down 8.1%**. `ece` = calibration error of confident calls (lower = probabilities more trustworthy).

| model | up@1 | up@5 | down@1 | down@5 | ece |
|---|---|---|---|---|---|
| **`baseline_random`** | 9% (1.0×) | 9% (1.0×) | 8% (1.0×) | 8% (1.0×) | — |
| `logistic` | 24% (2.6×) | 20% (2.2×) | 17% (2.1×) | 16% (2.0×) | 47.0% |
| `histgbm` ⭐ | 22% (2.3×) | 19% (2.0×) | 23% (2.9×) | 18% (2.3×) | 42.9% |
