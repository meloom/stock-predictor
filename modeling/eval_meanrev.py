"""modeling/eval_meanrev.py — iteration 4: worst-window confident-wrong calls look
like mean-reversion at extremes — the model SHORTS oversold names that bounce
(April: ORCL +5%, TTD +7%, AVGO +4%) and BUYS overbought names that crash (semis
at ATH: MRVL/LRCX/AMAT). Hypothesis: filter daily picks by RSI — don't short
oversold (RSI low), don't buy overbought (RSI high). Does it raise precision?

RSI (tech.rsi14) is already a model input; here it's used as a selection-time
guard. PIT-safe (known at the close). Champion model unchanged.

Run:  python3 modeling/eval_meanrev.py
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


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)


def top1(score, is_class, dates, keep):
    by_day = defaultdict(list)
    for i, d in enumerate(dates):
        if keep[i]:
            by_day[d].append(i)
    picks = correct = 0
    for _, idxs in by_day.items():
        if not idxs:
            continue
        i = max(idxs, key=lambda i: score[i])
        picks += 1; correct += int(is_class[i])
    return (correct / picks if picks else None, picks)


def main():
    ds = H.load_full_dataset(1)
    p = ds["panel"]; X = p["X"]; y = np.asarray(p["y"], float); meta = p["meta"]
    names = p["feature_names"]; rsi_col = names.index("tech.rsi14")
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]; lab = H.make_labels(y, MOVE)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)

    us, uy, ds_, dy, dd, rsi = [], [], [], [], [], []
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nan_to_num(np.nanmean(Xg[tr], 0), nan=0.0)
        std = np.nanstd(Xg[tr], 0); std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.nan_to_num(np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std), nan=0.0)
        clf = _model().fit(Xz(Xg[tr]), lab[tr]); cls = list(clf.classes_)
        pr = clf.predict_proba(Xz(Xg[dv]))
        us.append(pr[:, cls.index(1)] if 1 in cls else np.zeros(len(dv)))
        ds_.append(pr[:, cls.index(-1)] if -1 in cls else np.zeros(len(dv)))
        uy.append(lab[dv] == 1); dy.append(lab[dv] == -1)
        dd.extend(meta[i][0] for i in dv); rsi.extend(X[i, rsi_col] for i in dv)
    us, uy = np.concatenate(us), np.concatenate(uy)
    ds_, dy = np.concatenate(ds_), np.concatenate(dy)
    dd = np.array(dd); rsi = np.array(rsi, float)
    allkeep = np.ones(len(dd), bool)

    bu = top1(us, uy, dd, allkeep); bd = top1(ds_, dy, dd, allkeep)
    print(f"baseline: up@1 {bu[0]*100:.1f}% ({bu[1]})   down@1 {bd[0]*100:.1f}% ({bd[1]})\n")

    # diagnostic: is precision worse at the RSI extreme the direction 'fights'?
    ok = ~np.isnan(rsi)
    print("down-call precision by RSI (shorting LOW-RSI/oversold is the suspect):")
    for lo, hi, tag in [(0, 35, "oversold<35"), (35, 65, "mid"), (65, 101, "overbought>65")]:
        m = ok & (rsi >= lo) & (rsi < hi)
        d = top1(ds_, dy, dd, m)
        print(f"  RSI {tag:14s}: down@1 {(f'{d[0]*100:.1f}% ({d[1]})') if d[0] is not None else 'n/a'}")

    print("\nfilter (guard the extreme)      | up@1            | down@1")
    for th in (25, 30, 35):
        # don't SHORT if RSI < th (oversold); don't BUY if RSI > 100-th (overbought)
        keep_dn = ~(ok & (rsi < th))
        keep_up = ~(ok & (rsi > (100 - th)))
        u = top1(us, uy, dd, keep_up); d = top1(ds_, dy, dd, keep_dn)
        du = (u[0] - bu[0]) * 100; dv2 = (d[0] - bd[0]) * 100
        print(f"  no-short<{th} / no-buy>{100-th:<3d}      | {u[0]*100:5.1f}% ({u[1]}) {du:+.1f}pp | "
              f"{d[0]*100:5.1f}% ({d[1]}) {dv2:+.1f}pp")


if __name__ == "__main__":
    main()
