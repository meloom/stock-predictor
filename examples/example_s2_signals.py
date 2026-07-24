"""Example — S2 signal generation: one concrete input, confirmed output.

Input: 25 days of hand-made closes for two tickers (one rising, one falling).
Every expected value below is hand-computable from the input.
Run:  PYTHONPATH=src python3 examples/example_s2_signals.py
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import FeatureStore, MARKET_SCOPE
from s2_signals import run_signal_generation

# ── INPUT ────────────────────────────────────────────────────────────────
UP = [100 + i for i in range(25)]        # 100,101,...,124  (rises 1/day)
DOWN = [124 - i for i in range(25)]      # 124,123,...,100  (falls 1/day)
DAYS = [f"2026-06-{i+1:02d}" for i in range(25)]
LAST = DAYS[-1]                          # 2026-06-25

store = FeatureStore(Path(tempfile.mkdtemp()) / "example.db")
store.register("price.close", "float", "ticker", "S1", "daily", "post-close")
store.register("price.volume", "float", "ticker", "S1", "daily", "post-close")
rows = []
for d, u, dn in zip(DAYS, UP, DOWN):
    rows += [("price.close", "UP", d, float(u)), ("price.volume", "UP", d, 1e6),
             ("price.close", "DOWN", d, float(dn)), ("price.volume", "DOWN", d, 1e6)]
store.write_many(rows, trigger_id="example-seed")
print(f"INPUT: 25 daily closes  UP: 100→124 rising  DOWN: 124→100 falling  last day {LAST}")

# ── RUN ──────────────────────────────────────────────────────────────────
metrics = run_signal_generation(["UP", "DOWN"], LAST, store=store)

# ── OUTPUT ───────────────────────────────────────────────────────────────
up_mom5 = store.read_asof("tech.mom5", "UP", LAST)["value"]
up_rsi = store.read_asof("tech.rsi14", "UP", LAST)["value"]
down_rsi = store.read_asof("tech.rsi14", "DOWN", LAST)["value"]
rank_up = store.read_asof("xsec.rank_mom5", "UP", LAST)["value"]
breadth = store.read_asof("regime.breadth5", MARKET_SCOPE, LAST)["value"]
print(f"\nOUTPUT: UP mom5={up_mom5}  UP rsi14={up_rsi}  DOWN rsi14={down_rsi}")
print(f"        xsec.rank_mom5(UP)={rank_up}  regime.breadth5={breadth}")

# ── CONFIRM (hand-computed expectations) ─────────────────────────────────
assert abs(up_mom5 - (124/119 - 1)) < 1e-6,  "mom5 = 124/119-1 = +4.2017%"
assert up_rsi == 100.0,                      "monotonic riser -> RSI 100"
assert down_rsi == 0.0,                      "monotonic faller -> RSI 0"
assert rank_up == 1.0,                       "UP is the best of 2 -> rank 1.0"
assert breadth == 0.5,                       "1 of 2 tickers positive -> 0.5"
assert metrics["features_written"] == 15,    "5 tech x2 + 2 xsec ranks x2 + 1 breadth = 15"
print("\nCONFIRMED: every output matches its hand-computed expectation.")
