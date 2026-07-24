"""Example — S1 data ingestion, on REAL market data (not fakes).

Fetches real bars + macro + earnings calendar via yfinance, runs the real
ingestion, and saves:
    examples/s1_data.input.json    what was actually fetched (real)
    examples/s1_data.output.json   what was actually stored (real)
This is a dated snapshot — real data moves, so values differ by run day.
Run:  PYTHONPATH=src python3 examples/example_s1_data.py
"""
import sys, json, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from core import FeatureStore, MARKET_SCOPE
from s1_data import (run_daily_ingestion, fetch_daily_bars, fetch_macro,
                     fetch_days_to_earnings)

UNIVERSE = ["INTC", "MRVL", "TSLA"]

# ── fetch REAL data, capture it as the input snapshot ─────────────────────
real_bars = fetch_daily_bars(UNIVERSE, period="90d")
real_macro = fetch_macro(period="90d")
real_dte = {t: fetch_days_to_earnings(t) for t in UNIVERSE}

INPUT = {
    "note": "REAL yfinance data, snapshot. Bars/macro trimmed to last 5 rows "
            "for display; full history was ingested (see output counts).",
    "universe": UNIVERSE,
    "bars_tail": {t: rows[-5:] for t, rows in real_bars.items()},
    "macro_tail": {k: s[-5:] for k, s in real_macro.items()},
    "days_to_earnings": real_dte,
}
(HERE / "s1_data.input.json").write_text(json.dumps(INPUT, indent=2))

# ── run the REAL ingestion ────────────────────────────────────────────────
store = FeatureStore(Path(tempfile.mkdtemp()) / "example.db")
metrics = run_daily_ingestion(
    UNIVERSE, store=store,
    fetch_bars=lambda t: real_bars, fetch_macro=lambda: real_macro,
    fetch_dte=lambda t, asof=None: real_dte.get(t))

# latest date actually present, to read back the freshest stored values
latest = max(r["date"] for rows in real_bars.values() for r in rows)

def latest_val(feature, scope):
    rec = store.read_asof(feature, scope, latest)
    return None if rec is None else rec["value"]

OUTPUT = {
    "as_of_date": latest,
    "run_metrics": {k: v for k, v in metrics.items() if k != "trigger_id"},
    "latest_stored_values": {
        f"price.close {t}": latest_val("price.close", t) for t in UNIVERSE
    } | {
        "macro.vix": latest_val("macro.vix", MARKET_SCOPE),
        "macro.spy_close": latest_val("macro.spy_close", MARKET_SCOPE),
        **{f"days_to_earnings {t}": latest_val("calendar.days_to_earnings", t)
           for t in UNIVERSE},
    },
    "outputs_of_trigger": store.outputs_of(metrics["trigger_id"])["features"],
}
(HERE / "s1_data.output.json").write_text(json.dumps(OUTPUT, indent=2))

print(f"as_of {latest}: coverage {metrics['coverage_pct']}%, "
      f"{store.outputs_of(metrics['trigger_id'])['total_values']} real values stored")
print("wrote examples/s1_data.input.json + s1_data.output.json (REAL data)")
