"""modeling/exp01_eod_return_models.py — Experiment 01.

Train DIFFERENT models on the same PIT-correct panel with the same purged
split, compare them honestly (return-IC + price-vs-naive), and promote the
best ONLY if it clears the bar. If none clear it, promote nothing — a valid,
honest outcome.

Models tried: Ridge (linear baseline) vs GradientBoosting (nonlinear).
Run:  python3 modeling/exp01_eod_return_models.py [--horizon 20] [--promote]
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness as H

HORIZON = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 20
DO_PROMOTE = "--promote" in sys.argv
UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMD", "INTC", "MRVL", "AVGO", "QCOM", "MU",
            "AMZN", "GOOGL", "META", "NFLX", "TSLA", "CRM", "ORCL", "ADBE", "NOW",
            "JPM", "BAC", "V", "MA", "XOM", "CVX", "JNJ", "PFE", "MRNA", "UNH",
            "HD", "COST", "WMT", "PG", "KO", "PEP", "DIS", "SHOP", "COIN", "RBLX"]


def main():
    from sklearn.linear_model import Ridge
    from sklearn.ensemble import GradientBoostingRegressor

    print(f"Preparing panel (horizon {HORIZON}d)...")
    prep = H.prepare_panel(UNIVERSE, horizon_days=HORIZON)
    panel, base = prep["panel"], prep["base_prices"]
    split = H.purged_split(panel["meta"])
    Xtr, ytr = panel["X"][split["train_idx"]], panel["y"][split["train_idx"]]
    print(f"panel {len(panel['y'])} rows | train {len(split['train_idx'])} "
          f"test {len(split['test_idx'])} | split {split['train_end']} -> {split['test_start']}")

    candidates = {
        "ridge": Ridge(alpha=10.0),
        "gbm": GradientBoostingRegressor(n_estimators=100, max_depth=3,
                                         learning_rate=0.03, subsample=0.8,
                                         random_state=42),
    }
    results = {}
    for name, est in candidates.items():
        trained = H.fit(Xtr, ytr, est)
        metrics = H.evaluate_all(trained, panel, base, split)
        results[name] = {"trained": trained, "metrics": metrics}
        r, p = metrics["return"], metrics["price"]
        print(f"\n[{name}]  IC {r['ic']:+.4f}  beats_null {r['beats_null']}  "
              f"| price MAPE {p['model']['mape_pct']:.2f}% vs naive "
              f"{p['naive_persistence']['mape_pct']:.2f}%  beats_naive "
              f"{p['model_beats_naive_rmse']} ({p['rmse_improvement_pct']:+.2f}%)")

    # pick the best by return IC, promote only if it clears the bar
    best = max(results, key=lambda n: results[n]["metrics"]["return"]["ic"] or -1)
    bm = results[best]["metrics"]
    print(f"\nbest by IC: {best}  |  meets promotion bar: {H.meets_bar(bm)}")
    if DO_PROMOTE and H.meets_bar(bm):
        H.promote(f"eod_return_h{HORIZON}_{best}", results[best]["trained"],
                  {"experiment": "modeling/exp01_eod_return_models.py",
                   "model_type": best, "horizon_days": HORIZON,
                   "universe_size": len(UNIVERSE),
                   "split": {"train_end": split["train_end"],
                             "test_start": split["test_start"],
                             "purge_days": H.PURGE_DAYS, "embargo_days": H.EMBARGO_DAYS},
                   "test_metrics": bm})
        print(f"PROMOTED eod_return_h{HORIZON}_{best} to models/")
    elif DO_PROMOTE:
        print("NOT promoted — did not clear the bar (honest outcome).")


if __name__ == "__main__":
    main()
