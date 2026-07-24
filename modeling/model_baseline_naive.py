"""modeling/model_baseline_naive.py — BASELINE: naive persistence.

Predicts forward return = 0, i.e. "tomorrow's price = today's price" — a
random walk. This is the baseline every real model must beat on price RMSE;
if a model can't beat this, it has learned nothing. Logged to
performance.log like any model so the reference RMSE is always on record.
Never promoted (it is the bar, not a candidate).

Run:  python3 modeling/model_baseline_naive.py [--horizon 1]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

MODEL_NAME = "baseline_naive"
HORIZON = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1


def build_estimator():
    from sklearn.dummy import DummyRegressor
    return DummyRegressor(strategy="constant", constant=0.0)   # return 0 = persistence


def main():
    prep = H.prepare_window(horizon_days=HORIZON)
    panel, base, split, ranges = (prep["panel"], prep["base_prices"],
                                  prep["split"], prep["ranges"])
    trained = H.fit(panel["X"][split["train_idx"]], panel["y"][split["train_idx"]],
                    build_estimator())
    metrics = H.evaluate_at(trained, panel, base, split["dev_idx"])
    rec = H.log_performance(MODEL_NAME, ranges, metrics, promoted=False,
                            extra={"horizon_days": HORIZON, "is_baseline": True})
    print(f"[{MODEL_NAME}] dev {ranges['dev_range']}  price RMSE "
          f"{metrics['price']['model']['rmse']:.3f}  (this IS the naive bar)")


if __name__ == "__main__":
    main()
