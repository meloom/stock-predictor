"""Example — S2 signal generation, on REAL market data.

Runs real S1 ingestion first, then real S2 signal computation, and saves:
    examples/s2_signals.input.json    the real close/volume series S2 read
    examples/s2_signals.output.json   the real signals S2 computed
Dated snapshot. Run:  PYTHONPATH=src python3 examples/example_s2_signals.py
"""
import sys, json, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from core import FeatureStore, MARKET_SCOPE
from s1_data import run_daily_ingestion, fetch_daily_bars, fetch_macro
from s2_signals import run_signal_generation, S2_FEATURES

UNIVERSE = ["INTC", "MRVL", "TSLA"]

# ── real S1 ingestion into a shared store ─────────────────────────────────
real_bars = fetch_daily_bars(UNIVERSE, period="90d")
store = FeatureStore(Path(tempfile.mkdtemp()) / "example.db")
run_daily_ingestion(UNIVERSE, store=store,
                    fetch_bars=lambda t: real_bars,
                    fetch_macro=lambda: fetch_macro(period="90d"),
                    fetch_dte=lambda t, asof=None: None)
event_date = max(r["date"] for rows in real_bars.values() for r in rows)

# ── INPUT snapshot: the real series S2 will read (last 25 closes/ticker) ───
INPUT = {
    "note": "REAL closes/volumes ingested by S1; last 25 rows/ticker shown.",
    "event_date": event_date,
    "closes_tail": {t: [r["close"] for r in real_bars[t][-25:]] for t in UNIVERSE},
}
(HERE / "s2_signals.input.json").write_text(json.dumps(INPUT, indent=2))

# ── run REAL S2 ────────────────────────────────────────────────────────────
metrics = run_signal_generation(UNIVERSE, event_date, store=store)

# ── OUTPUT snapshot: the real computed signals ────────────────────────────
tech_feats = [n for n, *_ in S2_FEATURES if n.startswith("tech.")]
OUTPUT = {
    "event_date": event_date,
    "run_metrics": {k: v for k, v in metrics.items() if k != "trigger_id"},
    "per_ticker": {
        t: {f: (store.read_asof(f, t, event_date) or {}).get("value")
            for f in tech_feats + ["xsec.rank_rsi14", "xsec.rank_mom5"]}
        for t in UNIVERSE
    },
    "regime.breadth5": (store.read_asof("regime.breadth5", MARKET_SCOPE, event_date) or {}).get("value"),
}
(HERE / "s2_signals.output.json").write_text(json.dumps(OUTPUT, indent=2))

print(f"as_of {event_date}: {metrics['features_written']} real signals computed")
for t in UNIVERSE:
    o = OUTPUT["per_ticker"][t]
    print(f"  {t:5s} rsi14={o['tech.rsi14']}  mom5={o['tech.mom5']}  rank_mom5={o['xsec.rank_mom5']}")
print("wrote examples/s2_signals.input.json + s2_signals.output.json (REAL data)")
