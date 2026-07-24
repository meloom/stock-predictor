"""Example — S3 Predictors: the EOD price predictor, on REAL data.

What a price predictor is actually judged on: predicted PRICE vs actual PRICE,
RMSE / MAE / MAPE — MEASURED AGAINST THE NAIVE PERSISTENCE BASELINE (predict
tomorrow's price = today's price). On a near-random-walk series the naive
baseline gives tiny error that looks like skill; a model that doesn't beat it
has learned nothing, however good its chart looks. So we report both and show
per-stock predicted-vs-actual.

Run:  PYTHONPATH=src python3 examples/example_s3_predictors.py
"""
import sys, json, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from core import FeatureStore
from s1_data import run_daily_ingestion, fetch_daily_bars, fetch_macro
from s2_signals import run_signal_generation
from s3_predictors import (assemble_panel, train, evaluate, evaluate_price,
                           predict_eod, EOD_HORIZON_DAYS)

UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMD", "INTC", "MRVL", "AVGO", "QCOM", "MU",
            "AMZN", "GOOGL", "META", "NFLX", "TSLA", "CRM", "ORCL", "ADBE", "NOW",
            "JPM", "BAC", "V", "MA", "XOM", "CVX", "JNJ", "PFE", "MRNA", "UNH",
            "HD", "COST", "WMT", "PG", "KO", "PEP", "DIS", "SHOP", "COIN", "RBLX"]
HORIZON = EOD_HORIZON_DAYS   # 1 = next-day EOD close: the literal price predictor
EMBARGO = 7

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
sample = usable[::3]
print(f"Computing S2 features across {len(sample)} historical dates...")
for d in sample:
    run_signal_generation(UNIVERSE, d, store=store)

print(f"Assembling PIT panel (horizon {HORIZON}d)...")
panel = assemble_panel(store, UNIVERSE, sample, horizon_days=HORIZON)
X, y, meta = panel["X"], panel["y"], panel["meta"]

uniq = sorted({d for d, _ in meta})
train_end = uniq[int(len(uniq) * 0.7)]
test_start = uniq[min(int(len(uniq) * 0.7) + EMBARGO, len(uniq) - 1)]
tr = [i for i, (d, _) in enumerate(meta) if d <= train_end]
te = [i for i, (d, _) in enumerate(meta) if d >= test_start]

trained = train(X[tr], y[tr])
pred_ret = list(__import__("s3_predictors")._predict_vec(trained, X[te]))

# base prices for the test rows (today's close) -> predicted / actual / naive price
base_prices, actual_ret = [], []
per_stock = []
for k, i in enumerate(te):
    d, t = meta[i]
    p0 = store.read_asof("price.close", t, d)["value"]
    base_prices.append(p0)
    actual_ret.append(y[i])
    if k < 8:  # show a few concrete predicted-vs-actual PRICES
        per_stock.append({
            "date": d, "ticker": t, "todays_close": round(p0, 2),
            "predicted_price": round(p0 * (1 + pred_ret[k]), 2),
            "actual_price": round(p0 * (1 + y[i]), 2),
            "naive_price(=today)": round(p0, 2)})

ret_metrics = evaluate(trained, X[te], y[te])
price_metrics = evaluate_price(pred_ret, actual_ret, base_prices)

OUTPUT = {
    "what_this_is": "A stock PRICE predictor: for each stock it predicts the "
        f"close {HORIZON} day(s) ahead. Judged on predicted-vs-actual PRICE "
        "error, AND against the naive 'tomorrow=today' baseline — the honest "
        "test of whether it beats a random walk.",
    "model": "end_of_day_price / Ridge", "horizon_days": HORIZON,
    "universe_size": len(UNIVERSE), "test_rows": len(te),
    "sample_predictions_predicted_vs_actual_price": per_stock,
    "price_error_metrics": price_metrics,
    "return_metrics_cross_sectional": ret_metrics,
}
(HERE / "s3_predictors.output.json").write_text(json.dumps(OUTPUT, indent=2))

pm = price_metrics
print("\n════ PRICE prediction (what a price predictor is judged on) ════")
print(f"  model  RMSE {pm['model']['rmse']:.3f}  MAE {pm['model']['mae']:.3f}  MAPE {pm['model']['mape_pct']:.2f}%")
print(f"  naive  RMSE {pm['naive_persistence']['rmse']:.3f}  MAE {pm['naive_persistence']['mae']:.3f}  MAPE {pm['naive_persistence']['mape_pct']:.2f}%")
print(f"  beats naive: {pm['model_beats_naive_rmse']}  (RMSE improvement {pm['rmse_improvement_pct']:+.2f}%)")
print("  sample predicted vs actual price:")
for r in per_stock[:5]:
    print(f"    {r['ticker']:5s} {r['date']}: pred ${r['predicted_price']}  actual ${r['actual_price']}  (today ${r['todays_close']})")
print("wrote examples/s3_predictors.output.json")
