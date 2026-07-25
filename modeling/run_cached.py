"""modeling/run_cached.py — evaluate the model roster on the CACHED panel
(no re-fetch), log each to performance.log, and refresh the models table in
modeling/README.md. Keeps a human-readable record of every model + its result.

Run:  python3 modeling/run_cached.py [--horizon 1]
"""
import json
import sys
from pathlib import Path

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H
import panel_cache

HORIZON = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1
README = H.MODELING_DIR / "README.md"
START, END = "<!-- MODELS:START -->", "<!-- MODELS:END -->"


def regression_roster():
    from sklearn.linear_model import Ridge, Lasso, ElasticNet
    from sklearn.ensemble import (RandomForestRegressor, ExtraTreesRegressor,
                                  HistGradientBoostingRegressor)
    from sklearn.dummy import DummyRegressor
    return [
        ("baseline_naive", DummyRegressor(strategy="constant", constant=0.0)),
        ("baseline_mean", DummyRegressor(strategy="mean")),
        ("ridge", Ridge(alpha=10.0)),
        ("lasso", Lasso(alpha=0.001)),
        ("elasticnet", ElasticNet(alpha=0.001, l1_ratio=0.5)),
        ("random_forest", RandomForestRegressor(n_estimators=120, max_depth=4, random_state=0, n_jobs=-1)),
        ("extra_trees", ExtraTreesRegressor(n_estimators=120, max_depth=5, random_state=0, n_jobs=-1)),
        ("histgbm", HistGradientBoostingRegressor(max_depth=3, learning_rate=0.03, max_iter=200, random_state=0)),
    ]


def main():
    prep = panel_cache.load_cached(HORIZON)
    if prep is None:
        raise SystemExit("no cached panel — run panel_cache.py first.")
    panel, base, ranges = prep["panel"], prep["base_prices"], prep["ranges"]
    meta = panel["meta"]
    tr_lo, tr_hi = ranges["train_range"]; dv_lo, dv_hi = ranges["dev_range"]
    tr = [i for i, (d, _) in enumerate(meta) if tr_lo <= d <= tr_hi]
    dv = [i for i, (d, _) in enumerate(meta) if dv_lo <= d <= dv_hi]

    H.PERF_LOG.write_text("")  # fresh roster
    for name, est in regression_roster():
        trained = H.fit(panel["X"][tr], panel["y"][tr], est)
        m = H.evaluate_at(trained, panel, base, dv)
        H.log_performance(name, ranges, m, promoted=False,
                          extra={"horizon_days": HORIZON, "model_type": "regression"})
    # classification model logs itself
    import model_updown_clf
    sys.argv = ["model_updown_clf.py", "--move", "0.03", "--horizon", str(HORIZON)]
    model_updown_clf.main()

    update_readme(ranges)
    print(f"logged {len(regression_roster())+1} models + refreshed {README.name}")


def update_readme(ranges):
    rows = [json.loads(l) for l in H.PERF_LOG.read_text().splitlines() if l.strip()]
    naive = next((r["dev_naive_price_rmse"] for r in rows if r.get("dev_naive_price_rmse")), None)
    lines = [
        f"Auto-generated from `performance.log`. Train **{ranges['train_range'][0]} → "
        f"{ranges['train_range'][1]}** · dev **{ranges['dev_range'][0]} → {ranges['dev_range'][1]}** · "
        f"{len(ranges['tickers'])} tickers · {len(ranges['features'])} features · "
        f"label `{ranges['label_strategy']}`.",
        "",
        "| model | type | key dev metric | vs baseline |",
        "|---|---|---|---|",
    ]
    for r in rows:
        typ = r.get("model_type", "regression")
        if typ == "classification":
            dp = r.get("op_down_precision")
            lift = r.get("down_precision_lift")
            metric = (f"down precision {dp*100:.0f}% @ conf {r.get('op_threshold')}"
                      if dp else "n/a")
            vs = f"{lift:.1f}× base rate" if lift else "—"
        else:
            pr = r.get("dev_price_rmse")
            metric = f"price RMSE {pr:.3f}" if pr is not None else "n/a"
            vs = (f"{(naive-pr)/naive*100:+.2f}% vs naive" if pr is not None and naive else "—")
        lines.append(f"| `{r['model']}` | {typ} | {metric} | {vs} |")
    table = "\n".join(lines)

    text = README.read_text() if README.exists() else "# modeling/\n"
    block = f"{START}\n{table}\n{END}"
    if START in text and END in text:
        pre = text.split(START)[0]; post = text.split(END)[1]
        text = pre + block + post
    else:
        text = text.rstrip() + "\n\n## Models tried (auto-logged)\n\n" + block + "\n"
    README.write_text(text)


if __name__ == "__main__":
    main()
