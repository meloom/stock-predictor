"""modeling/model_ridge.py — ONE model: Ridge (linear baseline).

Protocol (harness-enforced): 4-week train / 2-week dev, full universe,
end-of-day forward-return label. Trains, evaluates on dev, appends the run to
modeling/performance.log, and promotes to models/ only if it clears the bar.

Run:  python3 modeling/model_ridge.py [--horizon 1] [--promote]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

MODEL_NAME = "ridge"
HORIZON = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1
DO_PROMOTE = "--promote" in sys.argv


def build_estimator():
    from sklearn.linear_model import Ridge
    return Ridge(alpha=10.0)


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
                            extra={"horizon_days": HORIZON,
                                   "coefficients": trained["coefficients"]})
    print(f"[{MODEL_NAME}] train {ranges['train_range']} dev {ranges['dev_range']} "
          f"tickers {rec['n_tickers']}")
    hit = metrics['return']['direction_hit_rate']
    print(f"  dev price RMSE {metrics['price']['model']['rmse']:.3f} vs naive "
          f"{metrics['price']['naive_persistence']['rmse']:.3f}  "
          f"(beats_naive {metrics['price']['model_beats_naive_rmse']})  | "
          f"direction hit-rate {hit*100:.1f}%" if hit is not None else "  (no direction)")
    print(f"  meets bar: {H.meets_bar(metrics)}  promoted: {promoted}  -> logged to performance.log")


if __name__ == "__main__":
    main()
