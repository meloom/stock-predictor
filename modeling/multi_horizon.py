"""modeling/multi_horizon.py — train + evaluate one model family per PREDICTION
WINDOW (horizon), so the strategy can choose how long to hold.

The features are horizon-independent (computed from data up to day d); only the
LABEL changes (forward return over H trading days). So we build the feature panel
ONCE (the cached h1 dataset) and just relabel per horizon — no re-fetch.

For each horizon H in {1,2,3,5,6,7} we:
  - relabel: big move if the H-day forward return > +3% / < -3% (base rate rises
    with H — reported per horizon; precision is judged vs THAT base rate).
  - evaluate the model roster across rolling 4wk/2wk windows (purge = H) under the
    decided per-day precision@k metric, PLUS a calibration check (ECE) so the
    confidence the strategy consumes is trustworthy.
  - write modeling/h{H}/README.md (that window's models).
Then write the top-level modeling/README.md as the index across ALL windows.

Run:  python3 modeling/multi_horizon.py
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
HORIZONS = [1, 2, 3, 5, 6, 7]
KS = (1, 2, 5)
README = H.MODELING_DIR / "README.md"
START, END = "<!-- MODELS:START -->", "<!-- MODELS:END -->"


def roster():
    # two fast models for the horizon SWEEP: logistic (best at longer windows) and
    # histgbm (the live champion, ~ties gradient_boosting). RF/ExtraTrees/GBM are
    # slow; a per-window folder can still try them individually via eval scripts.
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import HistGradientBoostingClassifier
    return [
        ("logistic", lambda: LogisticRegression(max_iter=500, class_weight="balanced")),
        ("histgbm", lambda: HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)),
    ]


def relabel(meta, bars, hz):
    """Forward H-trading-day return per (date,ticker) from cached bars. PIT-safe:
    uses the close H sessions AHEAD; rows without that future bar -> nan (dropped)."""
    S = AF._series(bars)
    y = np.full(len(meta), np.nan)
    for r, (d, t) in enumerate(meta):
        s = S.get(t)
        if not s or d not in s["idx"]:
            continue
        i = s["idx"][d]
        if i + hz < len(s["c"]):
            y[r] = s["c"][i + hz] / s["c"][i] - 1.0
    return y


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


def ece(prob, hit, bins=10):
    """Expected calibration error over confident up/down calls (prob>=0.5)."""
    m = prob >= 0.5
    if m.sum() < 20:
        return None
    p, h = prob[m], hit[m]
    edges = np.linspace(0.5, 1.0, bins + 1)
    e = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        b = (p >= lo) & (p < hi) if hi < 1.0 else (p >= lo) & (p <= hi)
        if b.sum() == 0:
            continue
        e += (b.sum() / m.sum()) * abs(h[b].mean() - p[b].mean())
    return float(e)


def eval_horizon(X, meta, y, hz):
    lab = H.make_labels(y, MOVE)
    ok = ~np.isnan(y)
    good = [j for j in range(X.shape[1]) if not np.isnan(X[ok][:, j]).all()]
    Xg = X[:, good]
    wins = H.rolling_windows(meta, horizon_days=hz, step_days=10)
    base_up = float(np.mean(lab[ok] == 1)); base_dn = float(np.mean(lab[ok] == -1))
    rows = [{"model": "baseline_random", "base": True,
             "up": {k: base_up for k in KS}, "dn": {k: base_dn for k in KS}, "ece": None}]
    for name, mk in roster():
        us, uy, ds, dy, dd, up_hit = [], [], [], [], [], []
        for w in wins:
            tr, dv = np.array(w["train_idx"]), np.array(w["dev_idx"])
            tr = tr[ok[tr]]; dv = dv[ok[dv]]
            if len(tr) < 50 or len(dv) < 20:
                continue
            mean = np.nan_to_num(np.nanmean(Xg[tr], 0), nan=0.0)
            std = np.nanstd(Xg[tr], 0); std[(std == 0) | np.isnan(std)] = 1
            Xz = lambda A: np.nan_to_num(np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std), nan=0.0)
            clf = mk().fit(Xz(Xg[tr]), lab[tr]); cls = list(clf.classes_)
            pr = clf.predict_proba(Xz(Xg[dv]))
            pu = pr[:, cls.index(1)] if 1 in cls else np.zeros(len(dv))
            pd = pr[:, cls.index(-1)] if -1 in cls else np.zeros(len(dv))
            us.append(pu); uy.append(lab[dv] == 1); ds.append(pd); dy.append(lab[dv] == -1)
            dd.extend(meta[i][0] for i in dv)
        us, uy = np.concatenate(us), np.concatenate(uy)
        ds, dy = np.concatenate(ds), np.concatenate(dy)
        rows.append({"model": name, "base": False,
                     "up": per_day_topk(us, uy, dd), "dn": per_day_topk(ds, dy, dd),
                     "ece": ece(np.concatenate([us, ds]), np.concatenate([uy, dy]))})
    return rows, base_up, base_dn, int(ok.sum())


def _cell(v, base):
    return f"{v*100:.0f}% ({v/base:.1f}×)" if v is not None else "n/a"


def write_horizon_readme(hz, rows, base_up, base_dn, n):
    d = H.MODELING_DIR / f"h{hz}"; d.mkdir(exist_ok=True)
    lines = [
        f"# h{hz} — {hz}-trading-day forward window",
        "",
        f"Predict whether a name moves **> +3% / < −3% over the next {hz} "
        f"session(s)**. Metric: per-day precision@k (top-k conviction picks/day; "
        f"fraction that hit). {n} labeled rows. Random-pick base rate: "
        f"**up {base_up*100:.1f}%, down {base_dn*100:.1f}%**. `ece` = calibration "
        f"error of confident calls (lower = probabilities more trustworthy).",
        "",
        "| model | up@1 | up@5 | down@1 | down@5 | ece |",
        "|---|---|---|---|---|---|",
    ]
    best = max((r for r in rows if not r["base"]),
               key=lambda r: (r["dn"][1] or 0) + (r["up"][1] or 0))
    for r in rows:
        nm = f"**`{r['model']}`**" if r["base"] else (
            f"`{r['model']}` ⭐" if r is best else f"`{r['model']}`")
        e = f"{r['ece']*100:.1f}%" if r["ece"] is not None else "—"
        lines.append(f"| {nm} | {_cell(r['up'][1], base_up)} | {_cell(r['up'][5], base_up)} "
                     f"| {_cell(r['dn'][1], base_dn)} | {_cell(r['dn'][5], base_dn)} | {e} |")
    (d / "README.md").write_text("\n".join(lines) + "\n")
    return best


def write_index(summary):
    lines = [
        "**All models, all prediction windows.** Each row is the best model for a "
        "horizon (full roster in `modeling/h{H}/README.md`). Metric: per-day "
        "precision@1 (single best conviction pick/side per day) vs the horizon's "
        "random base rate. Longer windows have a higher base rate (more time to "
        "move 3%) — read the lift (×), not the raw %.",
        "",
        "| window | best model | up@1 | down@1 | base up/down | ece |",
        "|---|---|---|---|---|---|",
    ]
    for s in summary:
        b = s["best"]
        e = f"{b['ece']*100:.1f}%" if b["ece"] is not None else "—"
        lines.append(
            f"| [h{s['hz']}](h{s['hz']}/README.md) — {s['hz']}d | `{b['model']}` | "
            f"{_cell(b['up'][1], s['base_up'])} | {_cell(b['dn'][1], s['base_dn'])} | "
            f"{s['base_up']*100:.0f}%/{s['base_dn']*100:.0f}% | {e} |")
    table = "\n".join(lines)
    text = README.read_text()
    block = f"{START}\n{table}\n{END}"
    text = text.split(START)[0] + block + text.split(END)[1]
    README.write_text(text)


def main():
    ds = H.load_full_dataset(1)
    p = ds["panel"]; X = p["X"]; meta = p["meta"]
    bars = AF.get_bars()
    summary = []
    print(f"{'window':8s} {'best model':18s} {'up@1':>12s} {'down@1':>12s} {'ece':>6s}")
    for hz in HORIZONS:
        y = relabel(meta, bars, hz)
        rows, bu, bd, n = eval_horizon(X, meta, y, hz)
        best = write_horizon_readme(hz, rows, bu, bd, n)
        summary.append({"hz": hz, "best": best, "base_up": bu, "base_dn": bd})
        up1 = f"{best['up'][1]*100:.0f}% ({best['up'][1]/bu:.1f}x)"
        dn1 = f"{best['dn'][1]*100:.0f}% ({best['dn'][1]/bd:.1f}x)"
        e = f"{best['ece']*100:.1f}%" if best['ece'] is not None else "—"
        print(f"h{hz:<7d} {best['model']:18s} {up1:>12s} {dn1:>12s} {e:>6s}")
    write_index(summary)
    print(f"\nwrote per-window READMEs (modeling/h*/README.md) + index (modeling/README.md)")


if __name__ == "__main__":
    main()
