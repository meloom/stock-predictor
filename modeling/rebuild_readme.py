"""modeling/rebuild_readme.py — regenerate the models table in modeling/README.md
under the DECIDED metric: per-day precision@k (pick the top-k names each day, %
that actually moved >±3%). Evaluates the whole model roster on the current full
dataset (which now includes the champion xh.* long-horizon block) across all
rolling 4wk/2wk windows, and writes the table with baseline_random as row 1.

RMSE and the old peak-precision columns are gone — this is the single metric.

Run:  python3 modeling/rebuild_readme.py
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

MOVE = H.MOVE_THRESHOLD
KS = (1, 2, 5, 10)
README = H.MODELING_DIR / "README.md"
START, END = "<!-- MODELS:START -->", "<!-- MODELS:END -->"


def roster():
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import (RandomForestClassifier, ExtraTreesClassifier,
                                  HistGradientBoostingClassifier, GradientBoostingClassifier)
    return [
        ("logistic", lambda: LogisticRegression(max_iter=500, class_weight="balanced")),
        ("random_forest", lambda: RandomForestClassifier(n_estimators=200, max_depth=5, random_state=0, n_jobs=-1, class_weight="balanced")),
        ("extra_trees", lambda: ExtraTreesClassifier(n_estimators=200, max_depth=6, random_state=0, n_jobs=-1, class_weight="balanced")),
        ("histgbm", lambda: HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)),
        ("gradient_boosting", lambda: GradientBoostingClassifier(n_estimators=150, max_depth=3, learning_rate=0.05, random_state=0)),
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


def evaluate(mk, X, lab, meta, wins):
    up_s, up_y, dn_s, dn_y, dates = [], [], [], [], []
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nan_to_num(np.nanmean(X[tr], 0), nan=0.0)
        std = np.nanstd(X[tr], 0); std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.nan_to_num(np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std),
                                     nan=0.0, posinf=0.0, neginf=0.0)
        clf = mk().fit(Xz(X[tr]), lab[tr]); cls = list(clf.classes_)
        proba = clf.predict_proba(Xz(X[dv]))
        up_s.append(proba[:, cls.index(1)] if 1 in cls else np.zeros(len(dv)))
        dn_s.append(proba[:, cls.index(-1)] if -1 in cls else np.zeros(len(dv)))
        up_y.append(lab[dv] == 1); dn_y.append(lab[dv] == -1)
        dates.extend(meta[i][0] for i in dv)
    up = per_day_topk(np.concatenate(up_s), np.concatenate(up_y), dates)
    dn = per_day_topk(np.concatenate(dn_s), np.concatenate(dn_y), dates)
    return up, dn


def main():
    ds = H.load_full_dataset(1)
    p = ds["panel"]; X = p["X"]; y = np.asarray(p["y"], float); meta = p["meta"]; names = p["feature_names"]
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]
    has_xh = any(names[j].startswith("xh.") for j in good)
    lab = H.make_labels(y, MOVE)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)
    base_up = float(np.mean(lab == 1)); base_dn = float(np.mean(lab == -1))

    rows = [{"model": "baseline_random", "base": True,
             "up": {k: base_up for k in KS}, "dn": {k: base_dn for k in KS}}]
    for name, mk in roster():
        up, dn = evaluate(mk, Xg, lab, meta, wins)
        rows.append({"model": name, "base": False, "up": up, "dn": dn})
        H.PERF_LOG_write = None
        with open(H.PERF_LOG, "a") as f:
            f.write(json.dumps({"model": name, "metric": "per_day_precision_at_k",
                                "features": len(good), "has_xhorizon": has_xh,
                                "up": {str(k): up[k] for k in KS},
                                "dn": {str(k): dn[k] for k in KS},
                                "base_rate_up": base_up, "base_rate_down": base_dn}) + "\n")

    _write_readme(rows, len(good), has_xh, len(wins), base_up, base_dn)
    print(f"features={len(good)} (xhorizon={'yes' if has_xh else 'no'}) windows={len(wins)}")
    print(f"{'model':18s} {'up@1':>6s} {'up@5':>6s} {'dn@1':>6s} {'dn@5':>6s} {'dn@1 lift':>9s}")
    for r in rows:
        f = lambda v: f"{v*100:.0f}%" if v is not None else "n/a"
        lift = r["dn"][1] / base_dn if r["dn"][1] else None
        print(f"{r['model']:18s} {f(r['up'][1]):>6s} {f(r['up'][5]):>6s} "
              f"{f(r['dn'][1]):>6s} {f(r['dn'][5]):>6s} {(f'{lift:.1f}x' if lift else '-'):>9s}")


def _write_readme(rows, n_feat, has_xh, n_wins, base_up, base_dn):
    def cell(v, base):
        if v is None:
            return "n/a"
        return f"{v*100:.0f}% ({v/base:.1f}×)"
    lines = [
        f"**Metric: per-day precision@k.** Each trading day, rank the model's "
        f"confidence and take the top-k names (k highest-conviction longs, and "
        f"separately k shorts); precision@k = the fraction of those daily picks "
        f"that actually moved **> +3%** (up) / **< −3%** (down). This is the "
        f"realistic trading read — a handful of best ideas per day. RMSE and the "
        f"old peak-precision columns are retired.",
        "",
        f"Evaluated across **{n_wins} rolling 4wk-train / 2wk-dev windows**, "
        f"**{n_feat} features**{' (incl. the champion `xh.*` long-horizon block)' if has_xh else ''}. "
        f"**Baseline = random daily pick = the base rate: up {base_up*100:.1f}%, "
        f"down {base_dn*100:.1f}%** — beat these to have signal. Cells show "
        f"precision (lift × the base rate).",
        "",
        "| model | up@1 | up@5 | down@1 | down@5 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        nm = f"**`{r['model']}`**" if r["base"] else f"`{r['model']}`"
        lines.append(f"| {nm} | {cell(r['up'][1], base_up)} | {cell(r['up'][5], base_up)} "
                     f"| {cell(r['dn'][1], base_dn)} | {cell(r['dn'][5], base_dn)} |")
    table = "\n".join(lines)
    text = README.read_text() if README.exists() else "# modeling/\n"
    block = f"{START}\n{table}\n{END}"
    text = (text.split(START)[0] + block + text.split(END)[1]) if START in text and END in text \
        else text.rstrip() + "\n\n## Models tried (auto-logged)\n\n" + block + "\n"
    README.write_text(text)


if __name__ == "__main__":
    main()
