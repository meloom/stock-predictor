"""modeling/run_cached.py — evaluate the model roster on the CACHED panel under
the DECIDED metric: PRECISION on big moves (up>+3% / down<-3%), NOT RMSE.

Every model is a big-move classifier. For each we report up-precision and
down-precision at a high-confidence operating point, and the lift over the
random base rate. RMSE is gone. Logs each to performance.log and refreshes the
models table in modeling/README.md.

Run:  python3 modeling/run_cached.py [--move 0.03] [--horizon 1]
"""
import json
import sys
from pathlib import Path

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H
import panel_cache

MOVE = float(sys.argv[sys.argv.index("--move") + 1]) if "--move" in sys.argv else 0.03
HORIZON = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1
README = H.MODELING_DIR / "README.md"
START, END = "<!-- MODELS:START -->", "<!-- MODELS:END -->"


def roster():
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import (RandomForestClassifier, ExtraTreesClassifier,
                                  HistGradientBoostingClassifier, GradientBoostingClassifier)
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.neural_network import MLPClassifier
    return [
        ("logistic", LogisticRegression(max_iter=500, C=1.0, class_weight="balanced")),
        ("random_forest", RandomForestClassifier(n_estimators=200, max_depth=5, random_state=0, n_jobs=-1, class_weight="balanced")),
        ("extra_trees", ExtraTreesClassifier(n_estimators=200, max_depth=6, random_state=0, n_jobs=-1, class_weight="balanced")),
        ("histgbm", HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)),
        ("gradient_boosting", GradientBoostingClassifier(n_estimators=150, max_depth=3, learning_rate=0.05, random_state=0)),
        ("knn", KNeighborsClassifier(n_neighbors=50)),
        ("mlp", MLPClassifier(hidden_layer_sizes=(32, 16), max_iter=400, random_state=0)),
    ]


def precision_op(scores, is_class, min_calls=8):
    """Best precision over a threshold sweep with an adequate sample (>=min_calls)."""
    best = None
    for th in np.linspace(0.2, 0.8, 25):
        called = scores >= th
        n = int(called.sum())
        if n < min_calls:
            continue
        prec = float(np.mean(is_class[called]))
        if best is None or prec > best[0]:
            best = (prec, n, round(float(th), 2))
    return best  # (precision, n_calls, threshold) or None


def evaluate(clf, Xtr, ytr, Xdev, ydev, base_up, base_dn):
    clf.fit(Xtr, ytr)
    proba = clf.predict_proba(Xdev)
    cls = list(clf.classes_)
    P_up = proba[:, cls.index(1)] if 1 in cls else np.zeros(len(Xdev))
    P_dn = proba[:, cls.index(-1)] if -1 in cls else np.zeros(len(Xdev))
    up = precision_op(P_up, ydev == 1)
    dn = precision_op(P_dn, ydev == -1)
    return {"up": up, "dn": dn}


def main():
    prep = panel_cache.load_cached(HORIZON)
    if prep is None:
        raise SystemExit("no cached panel — run panel_cache.py first.")
    panel, ranges = prep["panel"], prep["ranges"]
    X, y, meta = panel["X"], panel["y"], panel["meta"]
    dates = np.array([d for d, _ in meta])
    tr_lo, tr_hi = ranges["train_range"]; dv_lo, dv_hi = ranges["dev_range"]
    tr = np.array([i for i, d in enumerate(dates) if tr_lo <= d <= tr_hi])
    dv = np.array([i for i, d in enumerate(dates) if dv_lo <= d <= dv_hi])
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]
    mean = np.nanmean(Xg[tr], 0); std = np.nanstd(Xg[tr], 0)
    std[(std == 0) | np.isnan(std)] = 1
    Xz = np.where(np.isnan(Xg), 0.0, (np.nan_to_num(Xg) - mean) / std)
    lab = np.where(y > MOVE, 1, np.where(y < -MOVE, -1, 0))
    base_up = float(np.mean(lab[dv] == 1)); base_dn = float(np.mean(lab[dv] == -1))

    H.PERF_LOG.write_text("")
    results = []
    for name, clf in roster():
        r = evaluate(clf, Xz[tr], lab[tr], Xz[dv], lab[dv], base_up, base_dn)
        rec = {"model": name, "type": "big_move_classifier", "move_threshold": MOVE,
               "base_rate_up": base_up, "base_rate_down": base_dn,
               "up_precision": r["up"][0] if r["up"] else None,
               "up_calls": r["up"][1] if r["up"] else 0,
               "up_threshold": r["up"][2] if r["up"] else None,
               "down_precision": r["dn"][0] if r["dn"] else None,
               "down_calls": r["dn"][1] if r["dn"] else 0,
               "down_threshold": r["dn"][2] if r["dn"] else None,
               "up_lift": (r["up"][0] / base_up) if r["up"] and base_up else None,
               "down_lift": (r["dn"][0] / base_dn) if r["dn"] and base_dn else None,
               "train_range": ranges["train_range"], "dev_range": ranges["dev_range"],
               "n_tickers": len(ranges["tickers"]), "features": ranges["features"]}
        with open(H.PERF_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        results.append(rec)

    update_readme(ranges, base_up, base_dn, results)
    print(f"metric = PRECISION on big moves (>±{MOVE:.0%}). base rate: up {base_up*100:.1f}%, down {base_dn*100:.1f}%")
    print(f"{'model':18s} {'up prec':>10s} {'down prec':>12s} {'down lift':>10s}")
    for r in results:
        up = f"{r['up_precision']*100:.0f}% ({r['up_calls']})" if r['up_precision'] is not None else "n/a"
        dn = f"{r['down_precision']*100:.0f}% ({r['down_calls']})" if r['down_precision'] is not None else "n/a"
        lift = f"{r['down_lift']:.1f}x" if r['down_lift'] else "—"
        print(f"{r['model']:18s} {up:>10s} {dn:>12s} {lift:>10s}")


def update_readme(ranges, base_up, base_dn, results):
    lines = [
        f"**Metric: PRECISION on big moves** (label up if next-day return > "
        f"+{results[0]['move_threshold']:.0%}, down if < -{results[0]['move_threshold']:.0%}, "
        f"else neutral). We only judge up/down calls the model is confident about; "
        f"a neutral flagged up/down is the costly error. (RMSE was dropped — it "
        f"measured market timing, not stock selection.)",
        "",
        f"Train **{ranges['train_range'][0]} → {ranges['train_range'][1]}** · dev "
        f"**{ranges['dev_range'][0]} → {ranges['dev_range'][1]}** · {len(ranges['tickers'])} "
        f"tickers · {len(ranges['features'])} features. Random-guess precision "
        f"(base rate): **up {base_up*100:.1f}%**, **down {base_dn*100:.1f}%** — beat these to have signal.",
        "",
        "| model | up precision (calls) | down precision (calls) | down lift vs base |",
        "|---|---|---|---|",
    ]
    def cell(prec, calls, th):
        return f"{prec*100:.0f}% ({calls} @conf {th})" if prec is not None else "no confident calls"
    for r in results:
        lift = f"**{r['down_lift']:.1f}×**" if r["down_lift"] else "—"
        lines.append(f"| `{r['model']}` | {cell(r['up_precision'], r['up_calls'], r['up_threshold'])} "
                     f"| {cell(r['down_precision'], r['down_calls'], r['down_threshold'])} | {lift} |")
    table = "\n".join(lines)
    text = README.read_text() if README.exists() else "# modeling/\n"
    block = f"{START}\n{table}\n{END}"
    if START in text and END in text:
        text = text.split(START)[0] + block + text.split(END)[1]
    else:
        text = text.rstrip() + "\n\n## Models tried (auto-logged)\n\n" + block + "\n"
    README.write_text(text)


if __name__ == "__main__":
    main()
