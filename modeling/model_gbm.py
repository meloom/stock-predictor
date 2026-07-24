"""modeling/model_gbm.py — ONE model: GradientBoosting (nonlinear).

Same harness-enforced protocol as every model file: 4-week train / 2-week dev,
full universe, end-of-day forward-return label. Logs to modeling/performance.log,
promotes only if it clears the bar.

Run:  python3 modeling/model_gbm.py [--horizon 1] [--promote]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

MODEL_NAME = "gbm"
HORIZON = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1
DO_PROMOTE = "--promote" in sys.argv


def build_estimator():
    from sklearn.ensemble import GradientBoostingRegressor
    return GradientBoostingRegressor(n_estimators=100, max_depth=3,
                                     learning_rate=0.03, subsample=0.8,
                                     random_state=42)


def main():
    prep = H.prepare_window(horizon_days=HORIZON)
    panel, base, split, ranges = (prep["panel"], prep["base_prices"],
                                  prep["split"], prep["ranges"])
    trained = H.fit(panel["X"][split["train_idx"]], panel["y"][split["train_idx"]],
                    build_estimator())
    metrics = H.evaluate_at(trained, panel, base, split["dev_idx"])
    promoted = DO_PROMOTE and H.meets_bar(metrics)
    if promoted:
        H.promote(f"{MODEL_NAME}_eod_h{HORIZON}", trained, ranges, metrics)
    rec = H.log_performance(MODEL_NAME, ranges, metrics, promoted,
                            extra={"horizon_days": HORIZON})
    print(f"[{MODEL_NAME}] train {ranges['train_range']} dev {ranges['dev_range']} "
          f"tickers {rec['n_tickers']}")
    print(f"  dev IC {metrics['return']['ic']:+.4f}  beats_null {metrics['return']['beats_null']}"
          f"  | price MAPE {metrics['price']['model']['mape_pct']:.2f}% vs naive "
          f"{metrics['price']['naive_persistence']['mape_pct']:.2f}%  "
          f"beats_naive {metrics['price']['model_beats_naive_rmse']}")
    print(f"  meets bar: {H.meets_bar(metrics)}  promoted: {promoted}  -> logged to performance.log")


if __name__ == "__main__":
    main()
