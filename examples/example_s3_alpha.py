"""Example — S3 alpha: one concrete input, confirmed output.

Input: a hand-made bullish market (breadth 0.8, VIX 15, flat SPY) plus one
ticker 1 day from earnings, one 45 days out, one with no calendar data.
Expected regime score is hand-computable: .4*0.8 + .3*1.0 + .3*0.5 = 0.77.
Run:  PYTHONPATH=src python3 examples/example_s3_alpha.py
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import FeatureStore, MARKET_SCOPE
from s3_alpha import run_alpha, score_stocks

# ── INPUT ────────────────────────────────────────────────────────────────
store = FeatureStore(Path(tempfile.mkdtemp()) / "example.db")
store.register("regime.breadth5", "float", "market", "S2", "daily", "pit")
store.register("macro.vix", "float", "market", "S1", "daily", "pit")
store.register("macro.spy_close", "float", "market", "S1", "daily", "pit")
store.register("calendar.days_to_earnings", "int", "ticker", "S1", "daily", "pit")

DAY = "2026-06-30"
rows = [("macro.spy_close", MARKET_SCOPE, f"2026-06-{i+1:02d}", 700.0) for i in range(20)]
rows += [("regime.breadth5", MARKET_SCOPE, DAY, 0.8),
         ("macro.vix", MARKET_SCOPE, DAY, 15.0),
         ("calendar.days_to_earnings", "EARN_SOON", DAY, 1),
         ("calendar.days_to_earnings", "EARN_FAR", DAY, 45)]
store.write_many(rows, trigger_id="example-seed")
print("INPUT: breadth5=0.8, VIX=15, SPY flat at 700 for 20 days;")
print("       EARN_SOON dte=1, EARN_FAR dte=45, NO_CAL has no calendar data")

# ── RUN ──────────────────────────────────────────────────────────────────
metrics = run_alpha(["EARN_SOON", "EARN_FAR", "NO_CAL"], DAY, store=store)

# ── OUTPUT ───────────────────────────────────────────────────────────────
regime = store.read_asof("alpha.regime", MARKET_SCOPE, DAY)["value"]
print(f"\nOUTPUT: regime score={regime['score']} decision={regime['decision']}")
print(f"        components={regime['components']}")
print(f"        lineage stamp={regime['inputs_max_ingested_at'][:19]}...")
print(f"        event risk counts={metrics['event_risk_counts']}")
print(f"        scorer status={score_stocks()['status']}")

# ── CONFIRM (hand-computed expectations) ─────────────────────────────────
assert regime["score"] == 0.77,          ".4*0.8 + .3*1.0 + .3*0.5 = 0.77"
assert regime["decision"] == "TRADE",    "0.77 >= threshold 0.6"
assert regime["inputs_max_ingested_at"] is not None, "decision must carry lineage"
er = {t: store.read_asof("alpha.event_risk", t, DAY)["value"]["level"]
      for t in ["EARN_SOON", "EARN_FAR", "NO_CAL"]}
assert er == {"EARN_SOON": "HIGH", "EARN_FAR": "LOW", "NO_CAL": "UNKNOWN"}, \
    "dte=1->HIGH, dte=45->LOW, missing->UNKNOWN (never silently LOW)"
assert score_stocks()["status"] == "DISABLED", "scorer stays gated until §5 passes"
print("\nCONFIRMED: regime score exact, lineage stamped, UNKNOWN honest, scorer gated.")
