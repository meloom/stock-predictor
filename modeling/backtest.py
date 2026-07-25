"""modeling/backtest.py — turn precision into an honest P&L estimate.

Each dev day: go long the top-k names by p_up and short the top-k by p_down
(dollar-neutral, equal weight), hold 1 trading day, realize the actual return.
Aggregate gross, then net of a range of round-trip cost assumptions. Reports mean
daily return, monthly (x21), annualized, hit rate, and Sharpe.

Heavy caveats (see the printout): 10-month backtest, one regime, 109 liquid names,
no slippage/market-impact model beyond a flat bps cost, uncalibrated probabilities,
daily full turnover. This is a sanity range, NOT a promise.

Run:  python3 modeling/backtest.py
"""
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np
warnings.filterwarnings("ignore", category=RuntimeWarning)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

MOVE = H.MOVE_THRESHOLD
TRADING_DAYS_MONTH = 21


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05,
                                          max_iter=250, random_state=0)


def daily_pnl(K, cost_bps_roundtrip):
    ds = H.load_full_dataset(1)
    p = ds["panel"]; X = p["X"]; y = np.asarray(p["y"], float); meta = p["meta"]
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]; lab = H.make_labels(y, MOVE)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)
    day_ret = defaultdict(lambda: {"long": [], "short": []})
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nan_to_num(np.nanmean(Xg[tr], 0), nan=0.0)
        std = np.nanstd(Xg[tr], 0); std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.nan_to_num(np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std), nan=0.0)
        clf = _model().fit(Xz(Xg[tr]), lab[tr]); cls = list(clf.classes_)
        proba = clf.predict_proba(Xz(Xg[dv]))
        pu = proba[:, cls.index(1)] if 1 in cls else np.zeros(len(dv))
        pd = proba[:, cls.index(-1)] if -1 in cls else np.zeros(len(dv))
        by_day = defaultdict(list)
        for k, i in enumerate(dv):
            by_day[meta[i][0]].append((k, i))
        for d, items in by_day.items():
            longs = sorted(items, key=lambda ki: -pu[ki[0]])[:K]
            shorts = sorted(items, key=lambda ki: -pd[ki[0]])[:K]
            day_ret[d]["long"] += [y[i] for _, i in longs]
            day_ret[d]["short"] += [y[i] for _, i in shorts]
    # per-day dollar-neutral return per $1 gross = 0.5*mean_long - 0.5*mean_short
    rets = []
    for d in sorted(day_ret):
        L = day_ret[d]["long"]; S = day_ret[d]["short"]
        if not L or not S:
            continue
        gross = 0.5 * np.mean(L) - 0.5 * np.mean(S)
        cost = (cost_bps_roundtrip / 1e4)          # full turnover of $1 gross/day
        rets.append(gross - cost)
    return np.array(rets)


def summarize(rets, label):
    if len(rets) == 0:
        print(f"{label}: no trades"); return
    mu = rets.mean(); sd = rets.std()
    monthly = mu * TRADING_DAYS_MONTH
    ann = mu * 252
    sharpe = (mu / sd * np.sqrt(252)) if sd > 0 else float("nan")
    hit = np.mean(rets > 0)
    print(f"{label:34s} day {mu*100:+.3f}%  month {monthly*100:+.2f}%  "
          f"ann {ann*100:+.1f}%  Sharpe {sharpe:+.2f}  up-days {hit*100:.0f}%")


def main():
    print("Long/short backtest — dollar-neutral, top-K per side, 1-day hold, "
          f"daily rebalance. Return is per $1 GROSS capital.\n")
    for K in (1, 2, 5):
        print(f"--- K={K} names per side ---")
        gross = daily_pnl(K, 0)
        summarize(gross, f"  gross (no costs)")
        for c in (5, 10, 20):
            summarize(daily_pnl(K, c), f"  net @ {c}bps round-trip/day")
        print()
    print("Caveats: 10-month single-regime backtest; 109 liquid names; flat bps "
          "cost (no real slippage/impact/borrow); uncalibrated probs; assumes you "
          "can fill at the close. Treat as an order-of-magnitude sanity check.")


if __name__ == "__main__":
    main()
