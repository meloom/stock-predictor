"""modeling/eval_augmented.py — does adding error-analysis feature blocks lift
big-move PRECISION under a REALISTIC trading rule?

Metric = per-day precision@k. Each dev day we rank the model's up-scores and
take the top-k names (our k highest-conviction longs that day); precision@k =
fraction of those daily picks that actually moved > +3%. Same for the down side
(top-k shorts, fraction that actually moved < -3%). k = 1, 2, 5, 10. This is how
a book actually trades — a handful of best ideas per day — not a global
confidence pool. We compare baseline 25 features vs baseline + the block.

Champion model = HistGradientBoosting, trained across the SAME rolling 4wk/2wk
windows. Feature blocks come from error analysis (see augment_features.py).

Run:  python3 modeling/eval_augmented.py --blocks ext
      python3 modeling/eval_augmented.py --blocks sector
      python3 modeling/eval_augmented.py --blocks ext,sector
"""
import json
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np
warnings.filterwarnings("ignore", category=RuntimeWarning)   # all-NaN early windows
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H
import augment_features as AF

MOVE = float(sys.argv[sys.argv.index("--move") + 1]) if "--move" in sys.argv else H.MOVE_THRESHOLD
HORIZON = 1
KS = (1, 2, 5, 10)
BLOCKS = (sys.argv[sys.argv.index("--blocks") + 1].split(",")
          if "--blocks" in sys.argv else ["ext"])


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05,
                                          max_iter=250, random_state=0)


def per_day_topk(scores, is_class, dates, ks=KS):
    """For each day, take the top-k highest-score names; precision@k = fraction
    of those picks that are actually in-class, pooled over all days.
    Returns {k: (precision, n_picks, n_correct)}."""
    by_day = defaultdict(list)
    for i, d in enumerate(dates):
        by_day[d].append(i)
    out = {}
    for k in ks:
        picks, correct = 0, 0
        for d, idxs in by_day.items():
            idxs = sorted(idxs, key=lambda i: -scores[i])[:k]
            for i in idxs:
                picks += 1
                correct += int(is_class[i])
        out[k] = (correct / picks if picks else None, picks, correct)
    return out


def per_day_top1_list(scores, is_class, dates, meta_arr, ret_arr):
    """The single highest-conviction pick each day, with its outcome (for eyeball)."""
    by_day = defaultdict(list)
    for i, d in enumerate(dates):
        by_day[d].append(i)
    rows = []
    for d in sorted(by_day):
        i = max(by_day[d], key=lambda i: scores[i])
        rows.append((d, meta_arr[i][1], float(scores[i]), float(ret_arr[i]), bool(is_class[i])))
    return rows


def pooled(X, y, meta, feature_idx):
    """Train champion across all rolling windows; pool dev-day scores/labels."""
    lab = H.make_labels(y, MOVE)
    wins = H.rolling_windows(meta, horizon_days=HORIZON, step_days=10)
    Xg = X[:, feature_idx]
    up_s, up_y, dn_s, dn_y, dates, tick, ret = [], [], [], [], [], [], []
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nanmean(Xg[tr], 0); std = np.nanstd(Xg[tr], 0)
        std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std)
        clf = _model().fit(Xz(Xg[tr]), lab[tr])
        proba = clf.predict_proba(Xz(Xg[dv])); cls = list(clf.classes_)
        up_s.append(proba[:, cls.index(1)] if 1 in cls else np.zeros(len(dv)))
        dn_s.append(proba[:, cls.index(-1)] if -1 in cls else np.zeros(len(dv)))
        up_y.append(lab[dv] == 1); dn_y.append(lab[dv] == -1)
        dates.extend(meta[i][0] for i in dv); tick.extend(meta[i][1] for i in dv)
        ret.extend(y[i] for i in dv)
    up_s = np.concatenate(up_s); up_y = np.concatenate(up_y)
    dn_s = np.concatenate(dn_s); dn_y = np.concatenate(dn_y)
    meta_arr = list(zip(dates, tick)); ret = np.array(ret)
    return {"up": per_day_topk(up_s, up_y, dates), "dn": per_day_topk(dn_s, dn_y, dates),
            "up_top1": per_day_top1_list(up_s, up_y, dates, meta_arr, ret),
            "dn_top1": per_day_top1_list(dn_s, dn_y, dates, meta_arr, ret)}


def main():
    aug = AF.build_augmented(BLOCKS, HORIZON)
    X, y, meta = aug["X"], aug["y"], aug["meta"]
    base_n = len(aug["base_names"])
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    base_idx = [j for j in good if j < base_n]
    aug_idx = good
    lab = H.make_labels(y, MOVE)
    base_up = float(np.mean(lab == 1)); base_dn = float(np.mean(lab == -1))

    print(f"blocks added: {BLOCKS}  (+{len(aug['added_names'])}: {aug['added_names']})")
    print(f"base rate (random daily pick) — up {base_up*100:.1f}%  down {base_dn*100:.1f}%\n")
    B = pooled(X, y, meta, base_idx)
    A = pooled(X, y, meta, aug_idx)

    def table(side, label):
        print(f"per-day precision@k — {label} (pick top-k names/day):")
        print(f"  {'k':>3s} | {'base':>16s} | {'+'+'+'.join(BLOCKS):>16s} | {'Δpp':>6s} | {'lift vs random':>14s}")
        rep = {}
        base_rate = base_up if side == "up" else base_dn
        for k in KS:
            bp, bn, bc = B[side][k]; ap, an, ac = A[side][k]
            dp = (ap - bp) * 100
            print(f"  {k:>3d} | {bp*100:>6.1f}% ({bc:>3d}/{bn:<3d}) | {ap*100:>6.1f}% ({ac:>3d}/{an:<3d}) "
                  f"| {dp:>+5.1f} | {ap/base_rate:>12.2f}x")
            rep[k] = {"base": bp, "aug": ap}
        return rep
    up_rep = table("up", "UP  (long picks)")
    print()
    dn_rep = table("dn", "DOWN (short picks)")

    print("\nper-day TOP-1 UP pick (highest-conviction long each day) — +block model:")
    for d, t, conf, ret, hit in A["up_top1"][-14:]:   # last ~3 weeks for brevity
        print(f"    {'OK ' if hit else 'XX '} {t:6s} {d}  conf={conf:.2f}  actual={ret*100:+.1f}%")

    up_lift = np.mean([up_rep[k]["aug"] - up_rep[k]["base"] for k in KS])
    dn_lift = np.mean([dn_rep[k]["aug"] - dn_rep[k]["base"] for k in KS])
    print(f"\nmean Δ precision@k — up {up_lift*100:+.1f}pp   down {dn_lift*100:+.1f}pp   "
          f"(combined {(up_lift+dn_lift)/2*100:+.1f}pp)")
    print("VERDICT:", "KEEP block (helps)" if (up_lift + dn_lift) > 0.005 else "DROP block (no real lift)")

    rec = {"experiment": "augment:" + "+".join(BLOCKS), "metric": "per_day_precision_at_k",
           "ks": list(KS), "added_features": aug["added_names"], "n_features": len(aug_idx),
           "up": {str(k): {"base": B["up"][k][0], "aug": A["up"][k][0]} for k in KS},
           "dn": {str(k): {"base": B["dn"][k][0], "aug": A["dn"][k][0]} for k in KS},
           "base_rate_up": base_up, "base_rate_down": base_dn,
           "up_lift_pp": float(up_lift * 100), "dn_lift_pp": float(dn_lift * 100)}
    with open(H.PERF_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"logged -> {H.PERF_LOG}")
    return rec


if __name__ == "__main__":
    main()
