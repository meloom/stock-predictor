"""modeling/eval_vol_regime.py — iterations 2-3: all three worst windows are
high-volatility regimes (earnings cluster, semis crash, April whipsaw) where the
model's confident directional calls get run over in BOTH directions. Hypothesis:
the model's precision is conditional on the volatility regime, and it should
abstain (or be down-weighted) when the tape is violent.

Diagnostic: split the champion's confident top-1 picks by the day's VIX tercile
and by trailing cross-sectional dispersion — is precision much worse in the high
tercile? Then test abstaining on high-vol days.

Vol signals are day-level and PIT-safe (VIX from S1 macro; dispersion = trailing
5-day cross-sectional std of returns, known at the close).

Run:  python3 modeling/eval_vol_regime.py
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


def _model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=250, random_state=0)


def vix_by_date():
    m = AF.get_macro("1y")
    return {pt["date"]: float(pt["value"]) for pt in m.get("vix", []) if pt.get("value") is not None}


def dispersion_by_date(meta, bars):
    """trailing 5-day mean of the cross-sectional std of daily returns, per date."""
    S = AF._series(bars)
    dates = sorted({d for d, _ in meta})
    # daily cross-sectional std of 1-day returns
    day_std = {}
    for d in dates:
        rets = []
        for t, s in S.items():
            i = s["idx"].get(d)
            if i and i >= 1:
                rets.append(s["c"][i] / s["c"][i - 1] - 1.0)
        if len(rets) > 5:
            day_std[d] = float(np.std(rets))
    sd = sorted(day_std)
    idx = {d: i for i, d in enumerate(sd)}
    out = {}
    for d in sd:
        j = idx[d]
        if j >= 5:
            out[d] = float(np.mean([day_std[sd[k]] for k in range(j - 4, j + 1)]))
    return out


def per_day_top1(scores, is_class, dates, keep=None):
    by_day = defaultdict(list)
    for i, d in enumerate(dates):
        if keep is not None and not keep[i]:
            continue
        by_day[d].append(i)
    picks = correct = 0
    for _, idxs in by_day.items():
        i = max(idxs, key=lambda i: scores[i])
        picks += 1; correct += int(is_class[i])
    return (correct / picks if picks else None, picks)


def main():
    ds = H.load_full_dataset(1)
    p = ds["panel"]; X = p["X"]; y = np.asarray(p["y"], float); meta = p["meta"]
    good = [j for j in range(X.shape[1]) if not np.isnan(X[:, j]).all()]
    Xg = X[:, good]; lab = H.make_labels(y, MOVE)
    bars = AF.get_bars()
    vix = vix_by_date(); disp = dispersion_by_date(meta, bars)
    wins = H.rolling_windows(meta, horizon_days=1, step_days=10)

    us, uy, ds_, dy, dd = [], [], [], [], []
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
        dd.extend(meta[i][0] for i in dv)
    us, uy = np.concatenate(us), np.concatenate(uy)
    ds_, dy = np.concatenate(ds_), np.concatenate(dy)
    dd = np.array(dd)

    for signame, dmap in (("VIX", vix), ("dispersion", disp)):
        vals = np.array([dmap.get(d, np.nan) for d in dd])
        ok = ~np.isnan(vals)
        if ok.sum() < 30:
            print(f"{signame}: insufficient coverage"); continue
        lo, hi = np.nanpercentile(vals[ok], [33, 66])
        terc = np.where(vals <= lo, "low", np.where(vals >= hi, "high", "mid"))
        print(f"\n=== top-1 precision by {signame} tercile ===")
        for band in ("low", "mid", "high"):
            mask = (terc == band) & ok
            up = per_day_top1(us, uy, dd, keep=mask); dn = per_day_top1(ds_, dy, dd, keep=mask)
            fu = f"{up[0]*100:.1f}% ({up[1]})" if up[0] is not None else "n/a"
            fd = f"{dn[0]*100:.1f}% ({dn[1]})" if dn[0] is not None else "n/a"
            print(f"  {signame} {band:4s}: up@1 {fu:>12s}   down@1 {fd:>12s}")
        # abstention test: keep only NON-high days
        keep = (terc != "high") & ok
        upA = per_day_top1(us, uy, dd, keep=keep); dnA = per_day_top1(ds_, dy, dd, keep=keep)
        upB = per_day_top1(us, uy, dd); dnB = per_day_top1(ds_, dy, dd)
        du = (upA[0] - upB[0]) * 100; dv2 = (dnA[0] - dnB[0]) * 100
        print(f"  abstain on high-{signame} days: up@1 {du:+.1f}pp, down@1 {dv2:+.1f}pp "
              f"({'HELPS' if du+dv2>1 else 'no lift'})")


if __name__ == "__main__":
    main()
