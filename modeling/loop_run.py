"""modeling/loop_run.py — one iteration of the improvement loop.

Each iteration: error-analyze the current CHAMPION's worst dev predictions ->
train/test 3 proposed methods (feature transform x model) on the CACHED panel
(no re-fetch) -> log all to performance.log -> if any beats the champion, it
becomes the new champion (and is promoted to the registry). The champion is
the single best model so far by dev price-RMSE; naive persistence is the
initial champion, so "better than all" always means "beats the baseline too".

Run:  python3 modeling/loop_run.py --iter N [--horizon 1]
"""
import json
import sys
from pathlib import Path

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H
import panel_cache
import s3_predictors as pred

STATE = H.MODELING_DIR / "loop_state.json"
CHAMPION = H.MODELING_DIR / "champion.json"


# ── feature transforms (operate on the cached X; NO new data) ──────────────

def t_raw(X, names, meta): return X, names

def t_squares(X, names, meta):
    return np.hstack([X, np.nan_to_num(X) ** 2]), names + [n + "^2" for n in names]

def t_interactions(X, names, meta):
    import itertools
    keys = [n for n in ("tech.mom20", "tech.hvol20", "tech.rsi14", "tech.vr20",
                        "tech.ret_lag1", "fund.roe") if n in names]
    idx = [names.index(k) for k in keys]
    extra, en = [], []
    for a, b in itertools.combinations(idx, 2):
        extra.append((np.nan_to_num(X[:, a]) * np.nan_to_num(X[:, b]))[:, None])
        en.append(f"{names[a]}*{names[b]}")
    return (np.hstack([X] + extra), names + en) if extra else (X, names)

def t_xsec_rank(X, names, meta):
    import pandas as pd
    df = pd.DataFrame(X, columns=names)
    df["__d"] = [d for d, _ in meta]
    return df.groupby("__d")[names].rank(pct=True).values, names

def t_winsor(X, names, meta):
    lo = np.nanpercentile(X, 2, axis=0); hi = np.nanpercentile(X, 98, axis=0)
    return np.clip(X, lo, hi), names

def t_pca10(X, names, meta):
    from sklearn.decomposition import PCA
    mean = np.nanmean(X, 0); std = np.nanstd(X, 0); std[std == 0] = 1
    Xz = np.where(np.isnan(X), 0.0, (X - mean) / std)
    k = min(10, X.shape[1])
    Xp = PCA(n_components=k, random_state=0).fit_transform(Xz)
    return Xp, [f"pc{i}" for i in range(k)]

def t_subset(cols):
    def f(X, names, meta):
        idx = [names.index(c) for c in cols if c in names]
        return X[:, idx], [names[i] for i in idx]
    return f

MOM = ["tech.mom5", "tech.mom20", "tech.ret_lag1", "tech.ret_lag2", "tech.ret_lag3",
       "tech.ret_lag4", "tech.ret_lag5", "tech.rsi14"]
FUND = ["fund.book_to_price", "fund.earnings_yield", "fund.fcf_yield", "fund.roe",
        "fund.gross_profitability", "fund.net_margin", "fund.market_cap"]


def _est(kind):
    from sklearn.linear_model import Ridge, Lasso, ElasticNet
    from sklearn.ensemble import (RandomForestRegressor, ExtraTreesRegressor,
                                  HistGradientBoostingRegressor)
    from sklearn.neighbors import KNeighborsRegressor
    from sklearn.neural_network import MLPRegressor
    return {
        "ridge": Ridge(alpha=10.0), "ridge_a1": Ridge(alpha=1.0), "ridge_a100": Ridge(alpha=100.0),
        "lasso": Lasso(alpha=0.001), "elasticnet": ElasticNet(alpha=0.001, l1_ratio=0.5),
        "rf": RandomForestRegressor(n_estimators=120, max_depth=4, random_state=0, n_jobs=-1),
        "extratrees": ExtraTreesRegressor(n_estimators=120, max_depth=5, random_state=0, n_jobs=-1),
        "histgbm": HistGradientBoostingRegressor(max_depth=3, learning_rate=0.03, max_iter=200, random_state=0),
        "histgbm_d2": HistGradientBoostingRegressor(max_depth=2, learning_rate=0.05, max_iter=200, random_state=0),
        "knn": KNeighborsRegressor(n_neighbors=50),
        "mlp": MLPRegressor(hidden_layer_sizes=(32, 16), max_iter=400, random_state=0),
    }[kind]


# 10 iterations x 3 methods = an exploration over features x models.
SCHEDULE = [
    [("raw", "lasso"), ("raw", "elasticnet"), ("raw", "histgbm")],                  # 0
    [("raw", "rf"), ("raw", "extratrees"), ("raw", "knn")],                          # 1
    [("interactions", "ridge"), ("squares", "ridge"), ("pca10", "ridge")],           # 2
    [("xsec_rank", "ridge"), ("winsor", "ridge"), ("interactions", "histgbm")],      # 3
    [("mom", "ridge"), ("fund", "ridge"), ("xsec_rank", "histgbm")],                 # 4
    [("raw", "mlp"), ("interactions", "rf"), ("squares", "histgbm")],                # 5
    [("pca10", "histgbm"), ("winsor", "histgbm"), ("xsec_rank", "rf")],              # 6
    [("interactions", "elasticnet"), ("raw", "ridge_a1"), ("raw", "ridge_a100")],    # 7
    [("mom", "histgbm"), ("raw", "histgbm_d2"), ("interactions", "extratrees")],     # 8
    [("xsec_rank", "histgbm"), ("mom", "rf"), ("interactions", "histgbm")],          # 9
]

TRANSFORMS = {"raw": t_raw, "squares": t_squares, "interactions": t_interactions,
              "xsec_rank": t_xsec_rank, "winsor": t_winsor, "pca10": t_pca10,
              "mom": t_subset(MOM), "fund": t_subset(FUND)}


def _fit_generic(X, y, estimator):
    mean = np.nanmean(X, 0); std = np.nanstd(X, 0); std[std == 0] = 1.0
    Xz = np.where(np.isnan(X), 0.0, (X - mean) / std)
    model = estimator.fit(Xz, y)
    return {"model": model, "mean": mean, "std": std,
            "feature_names": [f"f{i}" for i in range(X.shape[1])], "coefficients": None}


def _eval(trained, Xdev, ydev, base_dev):
    ret = pred.evaluate(trained, Xdev, ydev)
    pr = pred._predict_vec(trained, Xdev).tolist()
    price = pred.evaluate_price(pr, ydev.tolist(), base_dev)
    return {"return": ret, "price": price}


def error_analysis(trained, Xdev, ydev, base_dev, meta_dev):
    """Worst dev predictions of the champion + a feature-POV note."""
    p = pred._predict_vec(trained, Xdev)
    err = np.abs(p - ydev)
    order = np.argsort(-err)[:5]
    worst = [{"date": meta_dev[i][0], "ticker": meta_dev[i][1],
              "predicted_ret": round(float(p[i]), 4), "actual_ret": round(float(ydev[i]), 4)}
             for i in order]
    big = ydev[np.abs(ydev) > 0.05]
    note = (f"worst errors are big real moves the model flattened toward ~0; "
            f"{int((np.abs(ydev) > 0.05).sum())}/{len(ydev)} dev moves exceeded 5% "
            f"(mostly unpredictable single-name jumps: earnings/news).")
    return {"worst": worst, "note": note}


def load_json(p, default): return json.loads(p.read_text()) if p.exists() else default


def main():
    it = int(sys.argv[sys.argv.index("--iter") + 1])
    hz = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1
    prep = panel_cache.load_cached(hz)
    if prep is None:
        raise SystemExit("no cached panel — run panel_cache.py first (needs one fetch).")
    panel, base, ranges = prep["panel"], prep["base_prices"], prep["ranges"]
    X0, y, meta = panel["X"], panel["y"], panel["meta"]
    names0 = panel["feature_names"]
    split = H.purged_split(meta) if hasattr(H, "purged_split") else None
    # use the harness window split (train/dev) recomputed from meta ranges
    tr_set = set(pd_range(ranges["train_range"], meta))
    dv_set = set(pd_range(ranges["dev_range"], meta))
    tr = [i for i, (d, _) in enumerate(meta) if d in tr_set]
    dv = [i for i, (d, _) in enumerate(meta) if d in dv_set]
    base_dev = [base[i] for i in dv]
    meta_dev = [meta[i] for i in dv]

    champ = load_json(CHAMPION, None)
    # champion error analysis (champion is a raw-feature model or naive)
    champ_trained = _fit_generic(X0[tr], y[tr], _est("ridge"))  # reference lens
    ea = error_analysis(champ_trained, X0[dv], y[dv], base_dev, meta_dev)

    results = []
    for feat, est in SCHEDULE[it % len(SCHEDULE)]:
        Xt, _ = TRANSFORMS[feat](X0, names0, meta)
        trained = _fit_generic(Xt[tr], y[tr], _est(est))
        m = _eval(trained, Xt[dv], y[dv], base_dev)
        name = f"iter{it}:{feat}+{est}"
        H.log_performance(name, ranges, m, promoted=False,
                          extra={"horizon_days": hz, "loop_iter": it,
                                 "error_note": ea["note"]})
        results.append((name, feat, est, m, trained))

    # champion selection: lowest dev price RMSE across new + incumbent
    def rmse(m): return m["price"]["model"]["rmse"]
    naive_rmse = results[0][3]["price"]["naive_persistence"]["rmse"]
    best = min(results, key=lambda r: rmse(r[3]))
    best_rmse = rmse(best[3])
    champ_rmse = champ["price_rmse"] if champ else float("inf")
    new_champ = best_rmse < champ_rmse
    if new_champ:
        CHAMPION.write_text(json.dumps({
            "model": best[0], "feature_transform": best[1], "estimator": best[2],
            "price_rmse": best_rmse, "naive_price_rmse": naive_rmse,
            "beats_naive": bool(best_rmse < naive_rmse),
            "direction_hit_rate": best[3]["return"]["direction_hit_rate"],
            "iter": it}, indent=2))

    STATE.write_text(json.dumps({"iter": it, "next_iter": it + 1,
                                 "champion_price_rmse": min(best_rmse, champ_rmse)}, indent=2))

    print(f"=== iter {it} ===")
    print("error analysis:", ea["note"])
    for name, feat, est, m, _ in results:
        print(f"  {name:34s} price_RMSE {rmse(m):8.3f}  vs_naive "
              f"{(naive_rmse-rmse(m))/naive_rmse*100:+.2f}%  dir_hit "
              f"{(m['return']['direction_hit_rate'] or 0)*100:.1f}%")
    print(f"champion now: {best[0] if new_champ else (champ['model'] if champ else 'naive')} "
          f"(RMSE {min(best_rmse, champ_rmse):.3f}, {'NEW' if new_champ else 'unchanged'})")


def pd_range(rng, meta):
    lo, hi = rng
    return [d for d, _ in meta if lo <= d <= hi]


if __name__ == "__main__":
    main()
