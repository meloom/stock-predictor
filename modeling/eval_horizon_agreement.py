"""modeling/eval_horizon_agreement.py — iteration 3: does cross-horizon AGREEMENT
make a better pick? A name the model calls up at h1 AND h2 AND h3 should be more
trustworthy than one it likes only at h1. Same features; three models trained on
1/2/3-day labels. Compare daily top-1 selected by h1-alone vs by combined scores
(mean / min across horizons) — all judged on the h1 (next-day) outcome.

Run:  python3 modeling/eval_horizon_agreement.py
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
HZ = [1, 2, 3]


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)


def relabel(meta, bars, hz):
    S = AF._series(bars); y = np.full(len(meta), np.nan)
    for r, (d, t) in enumerate(meta):
        s = S.get(t)
        if s and d in s["idx"] and s["idx"][d] + hz < len(s["c"]):
            i = s["idx"][d]; y[r] = s["c"][i + hz] / s["c"][i] - 1.0
    return y


def top1(score, is_class, dates):
    by_day = defaultdict(list)
    for i, d in enumerate(dates):
        by_day[d].append(i)
    picks = correct = 0
    for _, idxs in by_day.items():
        i = max(idxs, key=lambda i: score[i])
        picks += 1; correct += int(is_class[i])
    return correct / picks if picks else None, picks


def main():
    ds = H.load_full_dataset(1)
    p = ds["panel"]; X = p["X"]; y1 = np.asarray(p["y"], float); meta = p["meta"]
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]; bars = AF.get_bars()
    labs = {h: H.make_labels(relabel(meta, bars, h), MOVE) for h in HZ}
    y1lab = H.make_labels(y1, MOVE)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)

    # pooled per-horizon up/down probs on the SAME dev rows, plus the h1 label
    P = {h: {"up": [], "dn": []} for h in HZ}
    up1y, dn1y, dates = [], [], []
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nan_to_num(np.nanmean(Xg[tr], 0), nan=0.0)
        std = np.nanstd(Xg[tr], 0); std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.nan_to_num(np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std), nan=0.0)
        for h in HZ:
            lab = labs[h]
            m = _model().fit(Xz(Xg[tr]), lab[tr]); cls = list(m.classes_)
            pr = m.predict_proba(Xz(Xg[dv]))
            P[h]["up"].append(pr[:, cls.index(1)] if 1 in cls else np.zeros(len(dv)))
            P[h]["dn"].append(pr[:, cls.index(-1)] if -1 in cls else np.zeros(len(dv)))
        up1y.append(y1lab[dv] == 1); dn1y.append(y1lab[dv] == -1)
        dates.extend(meta[i][0] for i in dv)
    for h in HZ:
        P[h]["up"] = np.concatenate(P[h]["up"]); P[h]["dn"] = np.concatenate(P[h]["dn"])
    up1y = np.concatenate(up1y); dn1y = np.concatenate(dn1y); dates = np.array(dates)

    upmat = np.vstack([P[h]["up"] for h in HZ]); dnmat = np.vstack([P[h]["dn"] for h in HZ])
    scores = {
        "h1 alone (baseline)": (P[1]["up"], P[1]["dn"]),
        "mean(h1,h2,h3)": (upmat.mean(0), dnmat.mean(0)),
        "min(h1,h2,h3) [agreement]": (upmat.min(0), dnmat.min(0)),
    }
    print("selection score            | up@1 (judged on next-day outcome) | down@1")
    base_up = base_dn = None
    for name, (su, sd) in scores.items():
        u = top1(su, up1y, dates); d = top1(sd, dn1y, dates)
        if base_up is None:
            base_up, base_dn = u[0], d[0]
        du = (u[0] - base_up) * 100; dd = (d[0] - base_dn) * 100
        tag = "" if "baseline" in name else f"  (Δ up {du:+.1f}pp, down {dd:+.1f}pp)"
        print(f"{name:26s} | {u[0]*100:5.1f}% ({u[1]})                    | {d[0]*100:5.1f}% ({d[1]}){tag}")
    bu = (scores["min(h1,h2,h3) [agreement]"][0], up1y)
    a_up = top1(scores["min(h1,h2,h3) [agreement]"][0], up1y, dates)[0]
    a_dn = top1(scores["min(h1,h2,h3) [agreement]"][1], dn1y, dates)[0]
    print("\nVERDICT:", "AGREEMENT helps" if (a_up - base_up) + (a_dn - base_dn) > 0.01 else "no clear lift from agreement")


if __name__ == "__main__":
    main()
