"""Example — S1 data ingestion: one concrete input, confirmed output.

Runs OFFLINE with injected fake fetchers so it is deterministic and always
reproducible (the real network path is the default: run_daily_ingestion(universe)).
Run:  PYTHONPATH=src python3 examples/example_s1_data.py
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import FeatureStore, MARKET_SCOPE
from s1_data import run_daily_ingestion

# ── INPUT ────────────────────────────────────────────────────────────────
UNIVERSE = ["AAPL", "MSFT", "DEAD"]          # DEAD: a ticker whose fetch fails

def fake_bars(tickers):
    print(f"  fetch_bars called for {tickers}")
    return {"AAPL": [{"date": "2026-07-24", "close": 100.0, "volume": 1e6}],
            "MSFT": [{"date": "2026-07-24", "close": 380.0, "volume": 2e6}],
            "DEAD": []}                       # failed ticker -> empty, never fabricated

def fake_macro():
    return {"vix": [{"date": "2026-07-24", "value": 18.5}],
            "yield10y": [{"date": "2026-07-24", "value": 4.6}],
            "spy_close": [{"date": "2026-07-23", "value": 740.0},
                          {"date": "2026-07-24", "value": 738.0}]}

def fake_dte(ticker, asof=None):
    return {"AAPL": 90, "MSFT": 1}.get(ticker)   # DEAD -> None (no 999 sentinel)

# ── RUN ──────────────────────────────────────────────────────────────────
store = FeatureStore(Path(tempfile.mkdtemp()) / "example.db")
print("INPUT: universe =", UNIVERSE)
metrics = run_daily_ingestion(UNIVERSE, store=store, fetch_bars=fake_bars,
                              fetch_macro=fake_macro, fetch_dte=fake_dte)

# ── OUTPUT ───────────────────────────────────────────────────────────────
print("\nOUTPUT metrics:", {k: v for k, v in metrics.items() if k != "trigger_id"})
print("outputs_of(trigger):")
for f in store.outputs_of(metrics["trigger_id"])["features"]:
    print(f"  {f['feature']:28s} {f['n_values']} values")

# ── CONFIRM ──────────────────────────────────────────────────────────────
assert metrics["tickers_with_bars"] == 2,        "DEAD must not count as covered"
assert metrics["coverage_pct"] == 66.7,          "coverage must reflect the failure"
assert metrics["calendar_unknown"] == 1,         "DEAD's unknown dte counted, not faked"
assert store.read_asof("price.close", "AAPL", "2026-07-24")["value"] == 100.0
assert store.read_asof("macro.vix", MARKET_SCOPE, "2026-07-24")["value"] == 18.5
assert store.read_asof("calendar.days_to_earnings", "MSFT", "2026-07-24")["value"] == 1
assert store.read_asof("price.close", "DEAD", "2026-07-24") is None
print("\nCONFIRMED: coverage metrics honest about the failed ticker; "
      "all fetched values stored with lineage; nothing fabricated.")
