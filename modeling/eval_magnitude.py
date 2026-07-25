"""modeling/eval_magnitude.py — the RIGHT test for earnings features.

Earnings proximity can't predict the DIRECTION of a surprise, so it never lifts a
directional metric (precision@k). Its job is MAGNITUDE: "a big move is likely
here." That is Model 1 of the planned two-model split — a binary classifier for
P(|R| > 3%), direction-agnostic.

This trains that magnitude model with and without the `earnings` block and
reports, across the same rolling windows:
  - per-day precision@k: of the k names each day the model thinks most likely to
    move big, how many actually did (either direction)?
  - recall@0.5: of all real big moves, how many did the model flag (prob>=0.5)?
  - recall on the EARNINGS-reaction subset (moves within 1 day of a report) — the
    exact moves the directional model was blind to.

Run:  python3 modeling/eval_magnitude.py
"""
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np
warnings.filterwarnings("ignore", category=RuntimeWarning)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H
import augment_features as AF

MOVE = H.MOVE_THRESHOLD
KS = (1, 2, 5, 10)


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05,
                                          max_iter=250, random_state=0)


def per_day_topk(scores, is_big, dates, ks=KS):
    by_day = defaultdict(list)
    for i, d in enumerate(dates):
        by_day[d].append(i)
    out = {}
    for k in ks:
        picks = correct = 0
        for _, idxs in by_day.items():
            for i in sorted(idxs, key=lambda i: -scores[i])[:k]:
                picks += 1; correct += int(is_big[i])
        out[k] = correct / picks if picks else None
    return out


def run(X, feature_idx, y, meta, near_earn):
    """Train magnitude model P(|R|>3%) across windows; pool dev predictions."""
    big = (np.abs(y) > MOVE).astype(int)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)
    Xg = X[:, feature_idx]
    s_all, big_all, dates_all, near_all = [], [], [], []
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nan_to_num(np.nanmean(Xg[tr], 0), nan=0.0)
        std = np.nanstd(Xg[tr], 0); std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.nan_to_num(np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std),
                                     nan=0.0, posinf=0.0, neginf=0.0)
        clf = _model().fit(Xz(Xg[tr]), big[tr]); cls = list(clf.classes_)
        p_big = clf.predict_proba(Xz(Xg[dv]))[:, cls.index(1)] if 1 in cls else np.zeros(len(dv))
        s_all.append(p_big); big_all.append(big[dv])
        dates_all.extend(meta[i][0] for i in dv); near_all.append(near_earn[dv])
    s = np.concatenate(s_all); b = np.concatenate(big_all); ne = np.concatenate(near_all)
    topk = per_day_topk(s, b, dates_all)
    recall = float(np.mean(s[b == 1] >= 0.5)) if (b == 1).any() else 0.0
    # recall specifically on earnings-reaction big moves
    em = (b == 1) & (ne == 1)
    recall_earn = float(np.mean(s[em] >= 0.5)) if em.any() else float("nan")
    n_earn = int(em.sum())
    return {"topk": topk, "recall": recall, "recall_earn": recall_earn, "n_earn_moves": n_earn}


def main():
    aug = AF.build_augmented(["earnings"], 1)
    X, y, meta = aug["X"], aug["y"], aug["meta"]
    base_n = len(aug["base_names"])
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    base_idx = [j for j in good if j < base_n]              # champion features (incl xh.*)
    earn_idx = good                                        # + earnings block
    # near-earnings flag per row = eve.earn_imminent (report today/tomorrow) OR post_earn
    names = aug["feature_names"]
    ie = names.index("eve.earn_imminent"); ip = names.index("eve.post_earn")
    near = ((np.nan_to_num(X[:, ie]) == 1) | (np.nan_to_num(X[:, ip]) == 1)).astype(int)

    base_rate_big = float(np.mean(np.abs(y) > MOVE))
    print(f"MAGNITUDE model  P(|R|>3%)   base rate of big moves: {base_rate_big*100:.1f}%\n")
    B = run(X, base_idx, y, meta, near)
    A = run(X, earn_idx, y, meta, near)

    print(f"{'':22s} {'big@1':>7s} {'big@5':>7s} {'recall@.5':>10s} {'recall(earnings)':>17s}")
    def line(tag, r):
        print(f"{tag:22s} {r['topk'][1]*100:>6.1f}% {r['topk'][5]*100:>6.1f}% "
              f"{r['recall']*100:>9.1f}% {r['recall_earn']*100:>16.1f}%")
    line("champion (no earn)", B)
    line("+ earnings", A)
    print(f"\nearnings-reaction big moves in dev: {A['n_earn_moves']}")
    d1 = (A['topk'][1]-B['topk'][1])*100; dr = (A['recall']-B['recall'])*100
    de = (A['recall_earn']-B['recall_earn'])*100
    print(f"Δ big@1 {d1:+.1f}pp   Δ recall {dr:+.1f}pp   Δ recall(earnings) {de:+.1f}pp")
    print("VERDICT:", "earnings HELPS magnitude/recall" if (dr + de) > 1.0 else "no clear magnitude lift")


if __name__ == "__main__":
    main()
