"""modeling/debug_precision.py — precision measured across MANY rolling windows.

Debugging the "bad up precision": a single 2-week dev window is one regime. If
that fortnight fell, big up-moves are rare and unpredictable there — you cannot
fairly judge up-precision in it. This evaluates every model across ALL rolling
4wk/2wk windows in the standardized full dataset, POOLING the confident calls,
and splits the result by up-market vs down-market dev windows.

Includes the BASELINE explicitly: random-guess precision = the base rate.

Run:  python3 modeling/debug_precision.py [--move 0.03] [--horizon 1]
"""
import json
import sys
from pathlib import Path

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

MOVE = float(sys.argv[sys.argv.index("--move") + 1]) if "--move" in sys.argv else H.MOVE_THRESHOLD
HORIZON = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1
README = H.MODELING_DIR / "README.md"
START, END = "<!-- MODELS:START -->", "<!-- MODELS:END -->"


def roster():
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import (RandomForestClassifier, HistGradientBoostingClassifier,
                                  GradientBoostingClassifier, ExtraTreesClassifier)
    return [
        ("logistic", lambda: LogisticRegression(max_iter=500, class_weight="balanced")),
        ("random_forest", lambda: RandomForestClassifier(n_estimators=200, max_depth=5, random_state=0, n_jobs=-1, class_weight="balanced")),
        ("extra_trees", lambda: ExtraTreesClassifier(n_estimators=200, max_depth=6, random_state=0, n_jobs=-1, class_weight="balanced")),
        ("histgbm", lambda: HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)),
        ("gradient_boosting", lambda: GradientBoostingClassifier(n_estimators=150, max_depth=3, learning_rate=0.05, random_state=0)),
    ]


def peak_precision(scores, is_class, regime_mask=None, min_calls=15):
    """Best pooled precision over a threshold sweep with >= min_calls."""
    s = scores if regime_mask is None else scores[regime_mask]
    c = is_class if regime_mask is None else is_class[regime_mask]
    best = None
    for th in np.linspace(0.2, 0.85, 27):
        called = s >= th
        n = int(called.sum())
        if n < min_calls:
            continue
        p = float(np.mean(c[called]))
        if best is None or p > best[0]:
            best = (p, n, round(float(th), 2))
    return best


def main():
    ds = H.load_full_dataset(HORIZON)
    if ds is None:
        raise SystemExit("no full dataset — run harness.build_full_dataset first.")
    panel = ds["panel"]; X = panel["X"]; y = np.asarray(panel["y"], float); meta = panel["meta"]
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]
    lab = H.make_labels(y, MOVE)
    wins = H.rolling_windows(meta, horizon_days=HORIZON, step_days=10)
    print(f"full dataset: {len(y)} rows | rolling windows: {len(wins)} | move ±{MOVE:.0%}")

    # accumulate pooled (score,label,dev_up_regime) across all windows, per model
    pooled = {name: {"up_s": [], "up_y": [], "dn_s": [], "dn_y": [], "reg": []} for name, _ in roster()}
    all_dev_lab, all_dev_reg = [], []
    for w in wins:
        tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
        if len(tr) < 50 or len(dv) < 20:
            continue
        mean = np.nanmean(Xg[tr], 0); std = np.nanstd(Xg[tr], 0); std[(std == 0) | np.isnan(std)] = 1
        Xz = lambda A: np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std)
        dev_up_regime = y[dv].mean() > 0     # was the dev fortnight an up market?
        all_dev_lab.append(lab[dv]); all_dev_reg.append(np.full(len(dv), dev_up_regime))
        for name, mk in roster():
            clf = mk().fit(Xz(Xg[tr]), lab[tr])
            proba = clf.predict_proba(Xz(Xg[dv])); cls = list(clf.classes_)
            P_up = proba[:, cls.index(1)] if 1 in cls else np.zeros(len(dv))
            P_dn = proba[:, cls.index(-1)] if -1 in cls else np.zeros(len(dv))
            p = pooled[name]
            p["up_s"].append(P_up); p["up_y"].append(lab[dv] == 1)
            p["dn_s"].append(P_dn); p["dn_y"].append(lab[dv] == -1)
            p["reg"].append(np.full(len(dv), dev_up_regime))

    dev_lab = np.concatenate(all_dev_lab); dev_reg = np.concatenate(all_dev_reg)
    base_up = float(np.mean(dev_lab == 1)); base_dn = float(np.mean(dev_lab == -1))
    base_up_upmkt = float(np.mean(dev_lab[dev_reg] == 1)) if dev_reg.any() else float("nan")

    rows = [{"model": "baseline_random", "up_prec": base_up, "up_calls": None,
             "dn_prec": base_dn, "dn_calls": None, "up_prec_upmkt": base_up_upmkt,
             "up_lift": 1.0, "dn_lift": 1.0, "is_baseline": True}]
    for name, _ in roster():
        p = pooled[name]
        up_s = np.concatenate(p["up_s"]); up_y = np.concatenate(p["up_y"])
        dn_s = np.concatenate(p["dn_s"]); dn_y = np.concatenate(p["dn_y"])
        reg = np.concatenate(p["reg"])
        up = peak_precision(up_s, up_y)
        up_upmkt = peak_precision(up_s, up_y, regime_mask=reg)   # up-precision in UP markets only
        dn = peak_precision(dn_s, dn_y)
        rows.append({"model": name,
                     "up_prec": up[0] if up else None, "up_calls": up[1] if up else 0,
                     "up_prec_upmkt": up_upmkt[0] if up_upmkt else None,
                     "dn_prec": dn[0] if dn else None, "dn_calls": dn[1] if dn else 0,
                     "up_lift": (up[0]/base_up) if up and base_up else None,
                     "dn_lift": (dn[0]/base_dn) if dn and base_dn else None, "is_baseline": False})

    _log_and_readme(rows, ds, len(wins), base_up, base_dn, base_up_upmkt)
    print(f"\nBASELINE (random-guess) precision: up {base_up*100:.1f}%  down {base_dn*100:.1f}%  "
          f"(up in up-markets only: {base_up_upmkt*100:.1f}%)")
    print(f"{'model':18s} {'up prec':>9s} {'up(up-mkt)':>11s} {'down prec':>10s} {'dn lift':>8s}")
    for r in rows:
        f = lambda v: f"{v*100:.0f}%" if v is not None else "n/a"
        print(f"{r['model']:18s} {f(r['up_prec']):>9s} {f(r['up_prec_upmkt']):>11s} "
              f"{f(r['dn_prec']):>10s} {(str(round(r['dn_lift'],1))+'x') if r['dn_lift'] else '-':>8s}")


def _log_and_readme(rows, ds, n_wins, base_up, base_dn, base_up_upmkt):
    H.PERF_LOG.write_text("")
    for r in rows:
        with open(H.PERF_LOG, "a") as f:
            f.write(json.dumps({**r, "metric": "precision_big_moves", "n_windows": n_wins}) + "\n")
    lines = [
        f"**Metric: PRECISION on big moves** (up if fwd > +{MOVE:.0%}, down if < -{MOVE:.0%}). "
        f"Evaluated across **{n_wins} rolling 4wk-train/2wk-dev windows** (many regimes), "
        f"pooling confident calls. RMSE dropped (it measured market timing, not selection).",
        "",
        f"**Baseline = random-guess precision = the base rate: up {base_up*100:.1f}%, "
        f"down {base_dn*100:.1f}%.** Beat these to have signal. (Up-precision is also shown "
        f"restricted to UP-market dev windows, where up-moves aren't structurally rare — "
        f"base {base_up_upmkt*100:.1f}%.)",
        "",
        "| model | up precision | up precision (up-markets) | down precision | down lift vs base |",
        "|---|---|---|---|---|",
    ]
    def c(v, calls=None):
        if v is None: return "n/a"
        return f"{v*100:.0f}%" + (f" ({calls})" if calls else "")
    for r in rows:
        nm = f"**`{r['model']}`**" if r.get("is_baseline") else f"`{r['model']}`"
        lift = f"**{r['dn_lift']:.1f}×**" if r["dn_lift"] and not r.get("is_baseline") else ("1.0× (base)" if r.get("is_baseline") else "—")
        lines.append(f"| {nm} | {c(r['up_prec'], r.get('up_calls'))} | {c(r['up_prec_upmkt'])} "
                     f"| {c(r['dn_prec'], r.get('dn_calls'))} | {lift} |")
    table = "\n".join(lines)
    text = README.read_text() if README.exists() else "# modeling/\n"
    block = f"{START}\n{table}\n{END}"
    text = (text.split(START)[0] + block + text.split(END)[1]) if START in text and END in text \
        else text.rstrip() + "\n\n## Models tried (auto-logged)\n\n" + block + "\n"
    README.write_text(text)


if __name__ == "__main__":
    main()
