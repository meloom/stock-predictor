"""modeling/eval_earnings_abstain.py — iteration 1 hypothesis: the worst window
(2026-05-06..19) is an earnings cluster where the model's confident DIRECTIONAL
bets get run over by reports. Earnings can't predict direction, but we can ABSTAIN
— skip a daily pick if the name reports within `gap` sessions. Does that raise
per-day precision@k?

The earnings calendar is used ONLY as a selection-time filter (a strategy gate),
not as a model input — PIT-safe (report dates are scheduled ahead). Champion model
+ features unchanged.

Run:  python3 modeling/eval_earnings_abstain.py
"""
import sys
import warnings
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
warnings.filterwarnings("ignore", category=RuntimeWarning)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H
import augment_features as AF

MOVE = H.MOVE_THRESHOLD
KS = (1, 2, 5)


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)


def days_to_next_earn(meta):
    cal = AF.get_earnings()
    ords = {t: sorted(date(*map(int, d.split("-"))).toordinal() for d in ds) for t, ds in cal.items()}
    out = np.full(len(meta), 999)
    for r, (d, t) in enumerate(meta):
        es = ords.get(t)
        if not es:
            continue
        do = date(*map(int, d.split("-"))).toordinal()
        nxt = [e - do for e in es if e >= do]
        if nxt:
            out[r] = min(nxt)
    return out


def per_day_topk(scores, is_class, dates, exclude=None, ks=KS):
    by_day = defaultdict(list)
    for i, d in enumerate(dates):
        if exclude is not None and exclude[i]:
            continue
        by_day[d].append(i)
    out = {}
    for k in ks:
        picks = correct = 0
        for _, idxs in by_day.items():
            for i in sorted(idxs, key=lambda i: -scores[i])[:k]:
                picks += 1; correct += int(is_class[i])
        out[k] = (correct / picks if picks else None, picks)
    return out


def main():
    ds = H.load_full_dataset(1)
    p = ds["panel"]; X = p["X"]; y = np.asarray(p["y"], float); meta = p["meta"]
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]; lab = H.make_labels(y, MOVE)
    dte = days_to_next_earn(meta)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)

    us, uy, ds_, dy, dd, dte_pool = [], [], [], [], [], []
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
        dd.extend(meta[i][0] for i in dv); dte_pool.extend(dte[i] for i in dv)
    us, uy = np.concatenate(us), np.concatenate(uy)
    ds_, dy = np.concatenate(ds_), np.concatenate(dy)
    dte_pool = np.array(dte_pool)
    base_up = float(np.mean(lab == 1)); base_dn = float(np.mean(lab == -1))

    print(f"base rate up {base_up*100:.1f}% down {base_dn*100:.1f}%  "
          f"(near-earnings rows: <=1d {np.mean(dte_pool<=1)*100:.0f}%, <=2d {np.mean(dte_pool<=2)*100:.0f}%)\n")
    print(f"{'filter':16s} | {'up@1':>12s} {'up@5':>12s} | {'dn@1':>12s} {'dn@5':>12s}")
    results = {}
    for gap in (None, 1, 2, 3):
        ex = None if gap is None else (dte_pool <= gap)
        up = per_day_topk(us, uy, dd, ex); dn = per_day_topk(ds_, dy, dd, ex)
        tag = "none (baseline)" if gap is None else f"abstain <= {gap}d"
        f = lambda c: f"{c[0]*100:.1f}% ({c[1]})" if c[0] is not None else "n/a"
        print(f"{tag:16s} | {f(up[1]):>12s} {f(up[5]):>12s} | {f(dn[1]):>12s} {f(dn[5]):>12s}")
        results[gap] = {"up": up, "dn": dn}

    b = results[None]
    best = max((g for g in (1, 2, 3)),
               key=lambda g: (results[g]["up"][1][0] or 0) + (results[g]["dn"][1][0] or 0))
    du = (results[best]["up"][1][0] - b["up"][1][0]) * 100
    dd_ = (results[best]["dn"][1][0] - b["dn"][1][0]) * 100
    print(f"\nbest abstention = <= {best}d:  Δ up@1 {du:+.1f}pp   Δ down@1 {dd_:+.1f}pp")
    print("VERDICT:", "earnings-abstention HELPS precision" if (du + dd_) > 1.0 else "no clear precision lift")


if __name__ == "__main__":
    main()
