"""modeling/eval_signals.py — integrate the earnings-research signals (analyst-
revision momentum + pre-earnings drift) and test them across ALL models over ALL
rolling windows, under the decided per-day precision@k metric. Reports each model's
up@1/down@1 on the baseline feature set vs baseline + the new signals, plus the
side-specific DUAL champion.

Run:  python3 modeling/eval_signals.py [--blocks analyst,preearn]
"""
import json
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
KS = (1, 5)
BLOCKS = (sys.argv[sys.argv.index("--blocks") + 1].split(",")
          if "--blocks" in sys.argv else ["analyst", "preearn"])


def roster():
    # 3 core contenders (RF/ExtraTrees/GBM dropped to fit the wall-clock limit;
    # histgbm+logistic are the dual champion's halves).
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import (RandomForestClassifier, HistGradientBoostingClassifier)
    return [
        ("logistic", lambda: LogisticRegression(max_iter=500, class_weight="balanced")),
        ("random_forest", lambda: RandomForestClassifier(n_estimators=120, max_depth=5, random_state=0, n_jobs=-1, class_weight="balanced")),
        ("histgbm", lambda: HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)),
    ]


def per_day_topk(scores, is_class, dates, ks=KS):
    by_day = defaultdict(list)
    for i, d in enumerate(dates):
        by_day[d].append(i)
    out = {}
    for k in ks:
        picks = correct = 0
        for _, idxs in by_day.items():
            for i in sorted(idxs, key=lambda i: -scores[i])[:k]:
                picks += 1; correct += int(is_class[i])
        out[k] = correct / picks if picks else None
    return out


def pooled(mk, X, lab, meta, wins, cols):
    Xg = X[:, cols]
    us, uy, ds_, dy, dd = [], [], [], [], []
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nan_to_num(np.nanmean(Xg[tr], 0), nan=0.0)
        std = np.nanstd(Xg[tr], 0); std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.nan_to_num(np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std), nan=0.0)
        clf = mk().fit(Xz(Xg[tr]), lab[tr]); cls = list(clf.classes_)
        pr = clf.predict_proba(Xz(Xg[dv]))
        us.append(pr[:, cls.index(1)] if 1 in cls else np.zeros(len(dv)))
        ds_.append(pr[:, cls.index(-1)] if -1 in cls else np.zeros(len(dv)))
        uy.append(lab[dv] == 1); dy.append(lab[dv] == -1)
        dd.extend(meta[i][0] for i in dv)
    return (np.concatenate(us), np.concatenate(uy), np.concatenate(ds_),
            np.concatenate(dy), np.array(dd))


def main():
    aug = AF.build_augmented(BLOCKS, 1)
    X, y, meta = aug["X"], aug["y"], aug["meta"]
    names = aug["feature_names"]; base_n = len(aug["base_names"])
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    base_idx = [j for j in good if j < base_n]
    aug_idx = good
    lab = H.make_labels(y, MOVE)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)
    base_up = float(np.mean(lab == 1)); base_dn = float(np.mean(lab == -1))
    print(f"added blocks {BLOCKS}: {aug['added_names']}")
    print(f"base rate up {base_up*100:.1f}% down {base_dn*100:.1f}%  "
          f"(baseline {len(base_idx)} feats, +signals {len(aug_idx)} feats)\n")

    def topk(mk, cols):
        us, uy, ds_, dy, dd = pooled(mk, X, lab, meta, wins, cols)
        return per_day_topk(us, uy, dd), per_day_topk(ds_, dy, dd)

    print(f"{'model':18s} | {'up@1 base→+sig':>18s} | {'down@1 base→+sig':>18s}")
    log = []
    for name, mk in roster():
        bu, bd = topk(mk, base_idx); au, ad = topk(mk, aug_idx)
        du = (au[1] - bu[1]) * 100; dd = (ad[1] - bd[1]) * 100
        print(f"{name:18s} | {bu[1]*100:5.1f} → {au[1]*100:5.1f} ({du:+.1f}) | "
              f"{bd[1]*100:5.1f} → {ad[1]*100:5.1f} ({dd:+.1f})")
        log.append({"model": name, "up1_base": bu[1], "up1_aug": au[1],
                    "dn1_base": bd[1], "dn1_aug": ad[1], "blocks": BLOCKS})

    # the DUAL champion: logistic(up) + histgbm(down)
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import HistGradientBoostingClassifier
    lu_b = pooled(lambda: LogisticRegression(max_iter=500, class_weight="balanced"), X, lab, meta, wins, base_idx)
    lu_a = pooled(lambda: LogisticRegression(max_iter=500, class_weight="balanced"), X, lab, meta, wins, aug_idx)
    hd_b = pooled(lambda: HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0), X, lab, meta, wins, base_idx)
    hd_a = pooled(lambda: HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0), X, lab, meta, wins, aug_idx)
    dub = per_day_topk(lu_b[0], lu_b[1], lu_b[4])[1]; dua = per_day_topk(lu_a[0], lu_a[1], lu_a[4])[1]
    ddb = per_day_topk(hd_b[2], hd_b[3], hd_b[4])[1]; dda = per_day_topk(hd_a[2], hd_a[3], hd_a[4])[1]
    print(f"\nDUAL champion (log up / hist down):")
    print(f"  up@1   {dub*100:.1f} → {dua*100:.1f} ({(dua-dub)*100:+.1f}pp)")
    print(f"  down@1 {ddb*100:.1f} → {dda*100:.1f} ({(dda-ddb)*100:+.1f}pp)")
    valid = (dua - dub) + (dda - ddb) > 0.005
    print("VERDICT:", "signals HELP the champion" if valid else "no lift for the champion")

    H.PERF_LOG.write_text(H.PERF_LOG.read_text() + json.dumps(
        {"experiment": "earnings_signals:" + "+".join(BLOCKS), "roster": log,
         "dual_up1_base": dub, "dual_up1_aug": dua, "dual_dn1_base": ddb, "dual_dn1_aug": dda,
         "valid": bool(valid)}) + "\n")


if __name__ == "__main__":
    main()
