"""modeling/model_updown_clf.py — high-PRECISION big-move classifier.

Reframes the task: don't predict the exact return (a random walk at H=1) —
predict only the BIG moves, and only when confident. Labels: +1 if forward
return > +MOVE, -1 if < -MOVE, 0 (neutral) otherwise. We do NOT care about
neutral, but a neutral flagged up/down is a costly error -> PRECISION is the
metric, and we abstain below a confidence threshold.

This is the first framing that surfaced real signal: on the cached window the
DOWN side reaches ~50% precision vs an ~11.5% base rate (4x lift); UP is noise
in a falling dev window. Trains on the cached panel (no re-fetch), sweeps the
confidence threshold, logs the high-precision operating point + a plot.

Run:  python3 modeling/model_updown_clf.py [--move 0.03] [--horizon 1]
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


def precision_at(scores, actual_is_class, th, min_calls=3):
    called = scores >= th
    if called.sum() < min_calls:
        return None, int(called.sum())
    return float(np.mean(actual_is_class[called])), int(called.sum())


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
    Xz = lambda A: np.where(np.isnan(A), 0.0, (np.nan_to_num(A) - mean) / std)

    lab = np.where(y > MOVE, 1, np.where(y < -MOVE, -1, 0))

    from sklearn.ensemble import HistGradientBoostingClassifier
    clf = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05,
                                        max_iter=250, random_state=0).fit(Xz(Xg[tr]), lab[tr])
    proba = clf.predict_proba(Xz(Xg[dv])); cls = list(clf.classes_)
    P_up = proba[:, cls.index(1)]; P_dn = proba[:, cls.index(-1)]; ad = lab[dv]
    base_up = float(np.mean(ad == 1)); base_dn = float(np.mean(ad == -1))

    ths = [round(t, 2) for t in np.linspace(0.2, 0.8, 25)]
    sweep = []
    for th in ths:
        pu, nu = precision_at(P_up, ad == 1, th)
        pdn, ndn = precision_at(P_dn, ad == -1, th)
        both_n = (P_up >= th).sum() + (P_dn >= th).sum()
        both_c = ((ad[P_up >= th] == 1).sum() + (ad[P_dn >= th] == -1).sum())
        both = float(both_c / both_n) if both_n >= 3 else None
        sweep.append({"th": th, "up_prec": pu, "up_calls": nu,
                      "dn_prec": pdn, "dn_calls": ndn, "both_prec": both, "both_calls": int(both_n)})

    # operating point: best down-precision among thresholds with an adequate
    # sample (>= 8 down-calls) — avoids flattering thin high-threshold points.
    eligible = [s for s in sweep if s["dn_calls"] and s["dn_calls"] >= 8 and s["dn_prec"] is not None]
    op = max(eligible, key=lambda s: s["dn_prec"]) if eligible else sweep[len(sweep)//2]
    H.log_performance("updown_clf", ranges,
                      {"return": {"ic": None, "rmse": None, "direction_hit_rate": None, "beats_null": None},
                       "price": {"model": {"rmse": None, "mape_pct": None},
                                 "naive_persistence": {"rmse": None, "mape_pct": None},
                                 "model_beats_naive_rmse": None}},
                      promoted=False,
                      extra={"model_type": "classification", "move_threshold": MOVE,
                             "base_rate_up": base_up, "base_rate_down": base_dn,
                             "op_threshold": op["th"], "op_down_precision": op["dn_prec"],
                             "op_down_calls": op["dn_calls"], "op_up_precision": op["up_prec"],
                             "down_precision_lift": (op["dn_prec"] / base_dn) if op["dn_prec"] and base_dn else None})

    print(f"base rates — up>{MOVE:+.0%}: {base_up*100:.1f}%  down<-{MOVE:.0%}: {base_dn*100:.1f}%")
    print(f"{'thresh':>6s} {'UP calls':>8s} {'UP prec':>8s} {'DN calls':>8s} {'DN prec':>8s} {'BOTH prec':>9s}")
    for s in sweep[::3]:
        f = lambda v: f"{v*100:.1f}%" if v is not None else "   n/a"
        print(f"{s['th']:>6.2f} {s['up_calls']:>8d} {f(s['up_prec']):>8s} "
              f"{s['dn_calls']:>8d} {f(s['dn_prec']):>8s} {f(s['both_prec']):>9s}")
    lift = (op['dn_prec'] / base_dn) if op['dn_prec'] and base_dn else None
    print(f"operating point th={op['th']}: DOWN precision {op['dn_prec']*100:.1f}% "
          f"({op['dn_calls']} calls, {lift:.1f}x base rate)" if op['dn_prec'] else "no down operating point")


if __name__ == "__main__":
    main()
