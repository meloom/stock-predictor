"""Example — S3 alpha, on REAL market data.

Runs real S1 + S2 first, then the real S3 regime gate / event risk, and saves:
    examples/s3_alpha.input.json    the real features S3 read
    examples/s3_alpha.output.json   the real regime decision + event risk
Dated snapshot. Run:  PYTHONPATH=src python3 examples/example_s3_alpha.py
"""
import sys, json, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from core import FeatureStore, MARKET_SCOPE
from s1_data import run_daily_ingestion, fetch_daily_bars, fetch_macro, fetch_days_to_earnings
from s2_signals import run_signal_generation
from s3_alpha import run_alpha, score_stocks

UNIVERSE = ["INTC", "MRVL", "TSLA"]

# ── real S1 + S2 into a shared store ──────────────────────────────────────
real_bars = fetch_daily_bars(UNIVERSE, period="90d")
real_dte = {t: fetch_days_to_earnings(t) for t in UNIVERSE}
store = FeatureStore(Path(tempfile.mkdtemp()) / "example.db")
run_daily_ingestion(UNIVERSE, store=store,
                    fetch_bars=lambda t: real_bars,
                    fetch_macro=lambda: fetch_macro(period="90d"),
                    fetch_dte=lambda t, asof=None: real_dte.get(t))
event_date = max(r["date"] for rows in real_bars.values() for r in rows)
run_signal_generation(UNIVERSE, event_date, store=store)

# ── INPUT snapshot: the real features the regime gate reads ───────────────
spy = store.read_series("macro.spy_close", MARKET_SCOPE, event_date, 20)
INPUT = {
    "event_date": event_date,
    "regime.breadth5": (store.read_asof("regime.breadth5", MARKET_SCOPE, event_date) or {}).get("value"),
    "macro.vix": (store.read_asof("macro.vix", MARKET_SCOPE, event_date) or {}).get("value"),
    "spy_close_last20": [round(v, 2) for _, v in spy],
    "days_to_earnings": real_dte,
}
(HERE / "s3_alpha.input.json").write_text(json.dumps(INPUT, indent=2))

# ── run REAL S3 ────────────────────────────────────────────────────────────
metrics = run_alpha(UNIVERSE, event_date, store=store)


def _stable(obj):
    """Replace wall-clock ingested_at stamps with a placeholder so the saved
    snapshot only changes when real content changes — not on every re-run.
    The stamps are real in the live store; they're just noise in a committed
    example."""
    if isinstance(obj, dict):
        return {k: ("<ingested_at>" if k.endswith("ingested_at") and v else _stable(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stable(x) for x in obj]
    return obj

# ── OUTPUT snapshot: the real decision ────────────────────────────────────
regime = store.read_asof("alpha.regime", MARKET_SCOPE, event_date)["value"]
OUTPUT = _stable({
    "event_date": event_date,
    "regime": regime,
    "event_risk": {t: store.read_asof("alpha.event_risk", t, event_date)["value"]
                   for t in UNIVERSE},
    "scorer_status": score_stocks()["status"],
})
(HERE / "s3_alpha.output.json").write_text(json.dumps(OUTPUT, indent=2))

print(f"as_of {event_date}: regime {regime['decision']} (score {regime['score']}), "
      f"scorer {OUTPUT['scorer_status']}")
print("wrote examples/s3_alpha.input.json + s3_alpha.output.json (REAL data)")
