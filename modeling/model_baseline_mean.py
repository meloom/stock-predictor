"""modeling/model_baseline_mean.py — BASELINE: predict-the-mean (the null).

Predicts every stock's forward return = the training-set mean return. This is
the "null" baseline for the cross-sectional/return view: a model must beat its
MSE to show it predicts return MAGNITUDE better than a constant guess. Logged
to performance.log; never promoted (it is the bar, not a candidate).

Run:  python3 modeling/model_baseline_mean.py [--horizon 1]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

MODEL_NAME = "baseline_mean"
HORIZON = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1


def build_estimator():
    from sklearn.dummy import DummyRegressor
    return DummyRegressor(strategy="mean")   # predict the training mean return = null


def main():
    prep = H.prepare_window(horizon_days=HORIZON)
    panel, base, split, ranges = (prep["panel"], prep["base_prices"],
                                  prep["split"], prep["ranges"])
    trained = H.fit(panel["X"][split["train_idx"]], panel["y"][split["train_idx"]],
                    build_estimator())
    metrics = H.evaluate_at(trained, panel, base, split["dev_idx"])
    rec = H.log_performance(MODEL_NAME, ranges, metrics, promoted=False,
                            extra={"horizon_days": HORIZON, "is_baseline": True})
    print(f"[{MODEL_NAME}] dev {ranges['dev_range']}  return RMSE "
          f"{metrics['return']['rmse']:.5f}  (this IS the null bar)")


if __name__ == "__main__":
    main()
