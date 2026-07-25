"""modeling/extract_errors.py — the two failure modes of the champion, deeply.

PRECISION failures: the model's highest-confidence directional calls that were
WRONG (predicted up/down, didn't move that way). "What is the model wrongly
confident about?"

RECALL failures: the biggest ACTUAL big moves (|ret|>3%) the model MISSED —
gave a low probability to the correct direction. "What real moves is the model
blind to?" These expose information the feature set has no channel for.

Champion = HistGradientBoosting on the current full dataset (incl. xh.*), pooled
across all rolling 4wk/2wk dev windows. Writes modeling/error_examples2.json.

Run:  python3 modeling/extract_errors.py
"""
import json
import sys
import warnings
from pathlib import Path

import numpy as np
warnings.filterwarnings("ignore", category=RuntimeWarning)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

MOVE = H.MOVE_THRESHOLD


def main():
    ds = H.load_full_dataset(1)
    p = ds["panel"]; X = p["X"]; y = np.asarray(p["y"], float); meta = p["meta"]
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]
    lab = H.make_labels(y, MOVE)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)
    from sklearn.ensemble import HistGradientBoostingClassifier

    rows = []   # one dict per pooled dev prediction
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nan_to_num(np.nanmean(Xg[tr], 0), nan=0.0)
        std = np.nanstd(Xg[tr], 0); std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.nan_to_num(np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std),
                                     nan=0.0, posinf=0.0, neginf=0.0)
        clf = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05,
                                             max_iter=250, random_state=0).fit(Xz(Xg[tr]), lab[tr])
        proba = clf.predict_proba(Xz(Xg[dv])); cls = list(clf.classes_)
        iu = cls.index(1) if 1 in cls else None
        idn = cls.index(-1) if -1 in cls else None
        for k, i in enumerate(dv):
            d, t = meta[i]
            rows.append({"date": d, "ticker": t, "ret": float(y[i]), "label": int(lab[i]),
                         "p_up": float(proba[k, iu]) if iu is not None else 0.0,
                         "p_dn": float(proba[k, idn]) if idn is not None else 0.0})

    # ---- PRECISION failures: confident directional call that was wrong ----
    prec_fail = []
    for r in rows:
        if r["p_up"] >= 0.5 and r["label"] != 1:
            prec_fail.append({**r, "side": "UP", "conf": r["p_up"]})
        if r["p_dn"] >= 0.5 and r["label"] != -1:
            prec_fail.append({**r, "side": "DOWN", "conf": r["p_dn"]})
    prec_fail.sort(key=lambda r: -r["conf"])
    top_prec = prec_fail[:10]

    # ---- RECALL failures: biggest real moves given a LOW correct-class prob ----
    recall_fail = []
    for r in rows:
        if r["label"] == 1 and r["p_up"] < 0.3:
            recall_fail.append({**r, "side": "UP", "correct_prob": r["p_up"], "miss": abs(r["ret"])})
        if r["label"] == -1 and r["p_dn"] < 0.3:
            recall_fail.append({**r, "side": "DOWN", "correct_prob": r["p_dn"], "miss": abs(r["ret"])})
    recall_fail.sort(key=lambda r: -r["miss"])
    top_recall = recall_fail[:10]

    # recall base: what fraction of all real big moves did the model flag (prob>=0.5)?
    up_moves = [r for r in rows if r["label"] == 1]
    dn_moves = [r for r in rows if r["label"] == -1]
    up_recall = np.mean([r["p_up"] >= 0.5 for r in up_moves]) if up_moves else 0
    dn_recall = np.mean([r["p_dn"] >= 0.5 for r in dn_moves]) if dn_moves else 0

    print(f"pooled dev predictions: {len(rows)} | real up-moves {len(up_moves)} "
          f"(recall@0.5 {up_recall*100:.0f}%) | real down-moves {len(dn_moves)} "
          f"(recall@0.5 {dn_recall*100:.0f}%)\n")
    print("=== TOP 10 PRECISION FAILURES (confident call, actually wrong) ===")
    for r in top_prec:
        print(f"  {r['side']:4s} {r['ticker']:6s} {r['date']}  conf={r['conf']:.2f}  actual={r['ret']*100:+.1f}%")
    print("\n=== TOP 10 RECALL FAILURES (big real move the model missed) ===")
    for r in top_recall:
        print(f"  {r['side']:4s} {r['ticker']:6s} {r['date']}  actual={r['ret']*100:+.1f}%  "
              f"correct-class prob={r['correct_prob']:.2f}")

    json.dump({"precision_failures": top_prec, "recall_failures": top_recall,
               "up_recall_at_0.5": float(up_recall), "dn_recall_at_0.5": float(dn_recall),
               "n_up_moves": len(up_moves), "n_dn_moves": len(dn_moves)},
              open(H.MODELING_DIR / "error_examples2.json", "w"), indent=2)
    print("\nwrote modeling/error_examples2.json")


if __name__ == "__main__":
    main()
