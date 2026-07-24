"""Example — S3 Predictors: the EOD price predictor, trained & measured on REAL data.

Full honest pipeline: real 2y bars for a real universe -> S2 features across
~18 months of history -> PIT-correct panel (features as-of each date, forward
return target) -> purged train/test split -> train Ridge -> measure IC / MSE
vs. the predict-the-mean null on the held-out test. Saves the real result.

This is what makes it a predictor: a measured number, not a description.
Run:  PYTHONPATH=src python3 examples/example_s3_predictors.py
"""
import sys, json, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from core import FeatureStore
from s1_data import run_daily_ingestion, fetch_daily_bars, fetch_macro
from s2_signals import run_signal_generation
from s3_predictors import assemble_panel, train, evaluate, EOD_HORIZON_DAYS

UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMD", "INTC", "MRVL", "AVGO", "QCOM", "MU",
            "AMZN", "GOOGL", "META", "NFLX", "TSLA", "CRM", "ORCL", "ADBE", "NOW",
            "JPM", "BAC", "V", "MA", "XOM", "CVX", "JNJ", "PFE", "MRNA", "UNH",
            "HD", "COST", "WMT", "PG", "KO", "PEP", "DIS", "SHOP", "COIN", "RBLX"]
HORIZON = 20   # override EOD_HORIZON_DAYS(=1): ~monthly; daily is near-random-walk
PURGE, EMBARGO = 15, 7

print(f"Ingesting 2y bars for {len(UNIVERSE)} tickers...")
bars = fetch_daily_bars(UNIVERSE, period="2y")
store = FeatureStore(Path(tempfile.mkdtemp()) / "pred.db")
run_daily_ingestion(UNIVERSE, store=store, fetch_bars=lambda t: bars,
                    fetch_macro=lambda: fetch_macro("2y"),
                    fetch_dte=lambda t, asof=None: None, fetch_quote=lambda t: None,
                    fetch_shares=lambda t: None, fetch_statements=lambda t, asof=None: None,
                    fetch_analyst=lambda t: None)

all_dates = sorted({r["date"] for rows in bars.values() for r in rows})
usable = all_dates[50:-(HORIZON + 2)]
sample = usable[::5]
print(f"Computing S2 features across {len(sample)} historical dates...")
for d in sample:
    run_signal_generation(UNIVERSE, d, store=store)

print(f"Assembling PIT panel (horizon {HORIZON}d)...")
panel = assemble_panel(store, UNIVERSE, sample, horizon_days=HORIZON)
X, y, meta = panel["X"], panel["y"], panel["meta"]

uniq = sorted({d for d, _ in meta})
n = len(uniq)
train_end = uniq[int(n * 0.6)]
test_start = uniq[min(int(n * 0.8) + EMBARGO, n - 1)]
tr = [i for i, (d, _) in enumerate(meta) if d <= train_end]
te = [i for i, (d, _) in enumerate(meta) if d >= test_start]

trained = train(X[tr], y[tr])
ev = evaluate(trained, X[te], y[te])

OUTPUT = {
    "note": "REAL data. Technical features carry historical variation; the "
            "fundamental features are ~flat here (only the latest statement is "
            "ingested), so this is honestly a TECHNICAL-feature result until "
            "the S1 statement-history backfill lands.",
    "model": "end_of_day_price / Ridge", "horizon_days": HORIZON,
    "universe_size": len(UNIVERSE), "panel_rows": len(y),
    "train_rows": len(tr), "test_rows": len(te),
    "purged_split": {"train_end": train_end, "test_start": test_start,
                     "purge_days": PURGE, "embargo_days": EMBARGO},
    "test_metrics": ev,
    "top_factor_loadings": dict(sorted(trained["coefficients"].items(),
                                       key=lambda kv: -abs(kv[1]))[:6]),
}
(HERE / "s3_predictors.output.json").write_text(json.dumps(OUTPUT, indent=2))

print("\n════ REAL held-out test result (the answer to 'what are you predicting') ════")
print(f"  IC (rank corr):  {ev['ic']}")
print(f"  MSE {ev['mse']:.6f}  vs null {ev['null_mse']:.6f}   R2_vs_null {ev['r2_vs_null']:.5f}")
print(f"  beats null:      {ev['beats_null']}")
print(f"  top loadings:    {OUTPUT['top_factor_loadings']}")
print("wrote examples/s3_predictors.output.json")
