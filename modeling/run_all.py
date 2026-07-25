"""modeling/run_all.py — prepare the panel ONCE, evaluate every model on it.

Each model still lives in its own file (model_<name>.py defines build_estimator);
this runner just imports them so the whole comparison shares ONE data fetch and
ONE identical panel. Fixes: (a) hammering yfinance with N× fetches (rate limits),
(b) log races from parallel runs, (c) apples-to-apples — every model sees the
exact same train/dev window.

Run:  python3 modeling/run_all.py [--horizon 1] [--promote]
"""
import importlib
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

MODELS = ["baseline_naive", "baseline_mean", "ridge", "gbm"]
HORIZON = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1
DO_PROMOTE = "--promote" in sys.argv


def main():
    print(f"Preparing ONE panel (horizon {HORIZON}d, full universe)...")
    prep = H.prepare_window(horizon_days=HORIZON)
    panel, base, split, ranges = (prep["panel"], prep["base_prices"],
                                  prep["split"], prep["ranges"])
    print(f"panel {len(panel['y'])} rows | train {ranges['train_range']} "
          f"dev {ranges['dev_range']} | {len(ranges['tickers'])} tickers | "
          f"{len(ranges['features'])} features\n")

    naive_rmse = None
    for name in MODELS:
        mod = importlib.import_module(f"model_{name}")
        trained = H.fit(panel["X"][split["train_idx"]], panel["y"][split["train_idx"]],
                        mod.build_estimator())
        metrics = H.evaluate_at(trained, panel, base, split["dev_idx"])
        is_baseline = name.startswith("baseline_")
        promoted = DO_PROMOTE and not is_baseline and H.meets_bar(metrics)
        if promoted:
            H.promote(f"{name}_eod_h{HORIZON}", trained, ranges, metrics)
        H.log_performance(name, ranges, metrics, promoted,
                          extra={"horizon_days": HORIZON, "is_baseline": is_baseline})
        pr = metrics["price"]["model"]["rmse"]
        if name == "baseline_naive":
            naive_rmse = pr
        vs = f"{(naive_rmse - pr) / naive_rmse * 100:+.2f}%" if naive_rmse else "  base"
        hit = metrics["return"]["direction_hit_rate"]
        hs = f"{hit * 100:.1f}%" if hit is not None else " n/a"
        print(f"  {name:16s} price_RMSE {pr:8.3f}  vs_naive {vs:>7s}  "
              f"dir_hit {hs:>6s}  promoted {promoted}")


if __name__ == "__main__":
    main()
