"""modeling/worst_window.py — find the WORST dev window for the champion and dump
its errors, so each improvement-loop iteration analyzes a concrete bad period.

Walk-forward: each window trains on its own prior 4 weeks (no leakage). "Worst" =
the window where the top-1 long/short book lost the most (realized). For that
window we print the confident-wrong calls and the biggest MISSED moves — the raw
material for grounding what information the model lacked.

Run:  python3 modeling/worst_window.py [--rank N]   # N=0 worst, 1 second-worst...
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
RANK = int(sys.argv[sys.argv.index("--rank") + 1]) if "--rank" in sys.argv else 0


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)


def main():
    ds = H.load_full_dataset(1)
    p = ds["panel"]; X = p["X"]; y = np.asarray(p["y"], float); meta = p["meta"]
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]; lab = H.make_labels(y, MOVE)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)

    scored = []
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nan_to_num(np.nanmean(Xg[tr], 0), nan=0.0)
        std = np.nanstd(Xg[tr], 0); std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.nan_to_num(np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std), nan=0.0)
        clf = _model().fit(Xz(Xg[tr]), lab[tr]); cls = list(clf.classes_)
        pr = clf.predict_proba(Xz(Xg[dv]))
        pu = pr[:, cls.index(1)] if 1 in cls else np.zeros(len(dv))
        pd = pr[:, cls.index(-1)] if -1 in cls else np.zeros(len(dv))
        # realized top-1 long/short per day in this window
        by_day = defaultdict(list)
        for k, i in enumerate(dv):
            by_day[meta[i][0]].append((k, i))
        pnl = 0.0
        for d, items in by_day.items():
            lk = max(items, key=lambda ki: pu[ki[0]])[1]
            sk = max(items, key=lambda ki: pd[ki[0]])[1]
            pnl += 0.5 * y[lk] - 0.5 * y[sk]
        recs = [(meta[i], pu[k], pd[k], y[i], lab[i]) for k, i in enumerate(dv)]
        scored.append({"range": w["dev_range"], "pnl": pnl, "recs": recs})

    scored.sort(key=lambda s: s["pnl"])
    W = scored[RANK]
    print(f"WORST dev window (rank {RANK}): {W['range'][0]} .. {W['range'][1]}  "
          f"top-1 L/S realized P&L over window = {W['pnl']*100:+.1f}%  "
          f"(of {len(scored)} windows; best window {scored[-1]['pnl']*100:+.1f}%)\n")
    recs = W["recs"]
    pw = [(m, pu, pd, r, l) for (m, pu, pd, r, l) in recs if (pu >= 0.5 and l != 1) or (pd >= 0.5 and l != -1)]
    pw.sort(key=lambda x: -max(x[1], x[2]))
    print("CONFIDENT-WRONG calls in this window:")
    for (d, t), pu, pd, r, l in pw[:8]:
        side = "UP" if pu >= pd else "DOWN"; c = max(pu, pd)
        print(f"  {side:4s} {t:6s} {d}  conf={c:.2f}  actual={r*100:+.1f}%")
    miss = [(m, pu, pd, r, l) for (m, pu, pd, r, l) in recs if l == 1 and pu < 0.3 or l == -1 and pd < 0.3]
    miss.sort(key=lambda x: -abs(x[3]))
    print("\nBIGGEST MISSED moves in this window (model gave the right side <0.30):")
    for (d, t), pu, pd, r, l in miss[:8]:
        print(f"  {'UP' if l==1 else 'DOWN':4s} {t:6s} {d}  actual={r*100:+.1f}%  "
              f"p_correct={(pu if l==1 else pd):.2f}")


if __name__ == "__main__":
    main()
