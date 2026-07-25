# h6 — 6-trading-day forward window

Predict whether a name moves **> +3% / < −3% over the next 6 session(s)**. Metric: per-day precision@k (top-k conviction picks/day; fraction that hit). 22890 labeled rows. Random-pick base rate: **up 28.6%, down 24.8%**. `ece` = calibration error of confident calls (lower = probabilities more trustworthy).

| model | up@1 | up@5 | down@1 | down@5 | ece |
|---|---|---|---|---|---|
| **`baseline_random`** | 29% (1.0×) | 29% (1.0×) | 25% (1.0×) | 25% (1.0×) | — |
| `logistic` | 30% (1.1×) | 35% (1.2×) | 34% (1.4×) | 36% (1.4×) | 31.5% |
| `histgbm` ⭐ | 39% (1.4×) | 33% (1.1×) | 29% (1.2×) | 30% (1.2×) | 36.1% |
