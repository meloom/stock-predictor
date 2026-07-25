# h7 — 7-trading-day forward window

Predict whether a name moves **> +3% / < −3% over the next 7 session(s)**. Metric: per-day precision@k (top-k conviction picks/day; fraction that hit). 22781 labeled rows. Random-pick base rate: **up 30.1%, down 26.3%**. `ece` = calibration error of confident calls (lower = probabilities more trustworthy).

| model | up@1 | up@5 | down@1 | down@5 | ece |
|---|---|---|---|---|---|
| **`baseline_random`** | 30% (1.0×) | 30% (1.0×) | 26% (1.0×) | 26% (1.0×) | — |
| `logistic` ⭐ | 33% (1.1×) | 34% (1.1×) | 44% (1.7×) | 36% (1.4×) | 31.7% |
| `histgbm` | 35% (1.2×) | 37% (1.2×) | 29% (1.1×) | 32% (1.2×) | 35.3% |
