"""modeling/panel_cache.py — fetch the training panel ONCE, reuse for every
loop iteration. yfinance rate-limits repeated full-universe fetches hard, so
the error-analysis loop must NOT re-fetch each round — it loads this cache.
"""
import pickle
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

CACHE_DIR = H.ROOT / "runtime"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cache_path(horizon: int) -> Path:
    return CACHE_DIR / f"panel_cache_h{horizon}.pkl"


def build_and_cache(horizon: int = 1) -> dict:
    """One real fetch -> pickle the panel (arrays/lists only; no live store).
    Raises if yfinance is rate-limited (caller retries later)."""
    prep = H.prepare_window(horizon_days=horizon)
    prep.pop("split", None)  # recompute per-run; keep it small
    with open(cache_path(horizon), "wb") as f:
        pickle.dump(prep, f)
    return prep


def load_cached(horizon: int = 1):
    p = cache_path(horizon)
    if not p.exists():
        return None
    with open(p, "rb") as f:
        return pickle.load(f)


if __name__ == "__main__":
    hz = int(sys.argv[sys.argv.index("--horizon") + 1]) if "--horizon" in sys.argv else 1
    prep = build_and_cache(hz)
    print(f"cached panel: {len(prep['panel']['y'])} rows, "
          f"{len(prep['ranges']['tickers'])} tickers, "
          f"{len(prep['ranges']['features'])} features -> {cache_path(hz)}")
