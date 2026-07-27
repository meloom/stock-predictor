"""reportlog.py — freeze the day's PICKS / predictions to a persistent log, and SCORE
them later against realized returns to measure real out-of-sample performance.

The feature store already logs collected data / signals / predictions bitemporally
(event_time + ingested_at). What it doesn't do is snapshot the *report* (today's top &
bottom picks + per-ticker prediction) in a scoreable form. This does:

  snapshot()  → append today's universe predictions + which names were top-long / top-short
  score()     → once the horizon has elapsed (forward bars have arrived), compute the
                realized forward return per pick and the precision of that day's picks.

Usage:  python3 -m reportlog snapshot      # log today (run daily)
        python3 -m reportlog score         # score every matured snapshot
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import DEFAULT_DB                                       # noqa: E402
import pipeline_map                                              # noqa: E402

HORIZONS = [1, 3, 5, 7]
THR = 0.03


def _conn(db_path):
    c = sqlite3.connect(Path(db_path))
    c.execute("""CREATE TABLE IF NOT EXISTS report_snapshots(
        snapshot_date TEXT, as_of TEXT, ticker TEXT, price REAL, horizon INTEGER,
        p_up REAL, p_down REAL, rank_up INTEGER, rank_down INTEGER,
        in_long INTEGER, in_short INTEGER, generated_at TEXT NOT NULL,
        PRIMARY KEY (snapshot_date, ticker, horizon))""")
    return c


def snapshot(db_path=DEFAULT_DB) -> dict:
    """Freeze today's picks + per-ticker predictions (the deployed classifier at its
    latest date) into report_snapshots."""
    now = datetime.now(timezone.utc)
    snap = now.date().isoformat()
    scr = pipeline_map.alpha_screen(db_path, n=15)
    as_of = scr["as_of"]
    long_set = {x["ticker"] for x in scr["longs"]}
    short_set = {x["ticker"] for x in scr["shorts"]}
    c = _conn(db_path)

    def ranked(feat):
        md = c.execute("SELECT MAX(event_time) FROM feature_values WHERE feature=?", (feat,)).fetchone()
        if not md or not md[0]:
            return {}
        rk = sorted(((sc, float(v)) for sc, v, _ in c.execute(
            "SELECT scope, value, MAX(ingested_at) FROM feature_values WHERE feature=? "
            "AND event_time=? GROUP BY scope", (feat, md[0]))), key=lambda x: -x[1])
        return {sc: (v, i + 1) for i, (sc, v) in enumerate(rk)}

    up = {h: ranked(f"predict.pbig_up_{h}d") for h in HORIZONS}
    down = {h: ranked(f"predict.pbig_down_{h}d") for h in HORIZONS}
    px = {sc: float(v) for sc, v, _ in c.execute(
        "SELECT scope, value, MAX(ingested_at) FROM feature_values WHERE feature='price.close' "
        "AND event_time=? GROUP BY scope", (as_of,))}
    tickers = set().union(*[set(up[h]) for h in HORIZONS]) if any(up.values()) else set()
    rows = 0
    for t in tickers:
        for h in HORIZONS:
            pu, ru = up[h].get(t, (None, None))
            pd, rd = down[h].get(t, (None, None))
            c.execute("INSERT OR REPLACE INTO report_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (snap, as_of, t, px.get(t), h, pu, pd, ru, rd,
                       1 if t in long_set else 0, 1 if t in short_set else 0, now.isoformat()))
            rows += 1
    c.commit()
    return {"snapshot_date": snap, "as_of": as_of, "rows": rows,
            "longs": [x["ticker"] for x in scr["longs"]],
            "shorts": [x["ticker"] for x in scr["shorts"]]}


def _price_series(c):
    ser = {}
    for sc, et, v in c.execute("SELECT scope, event_time, value FROM feature_values "
                               "WHERE feature='price.close'"):
        try:
            ser.setdefault(sc, {})[et] = float(v)
        except (TypeError, ValueError):
            pass
    return {t: sorted(pm.items()) for t, pm in ser.items()}


def score(db_path=DEFAULT_DB) -> dict:
    """For every logged snapshot, once the horizon's forward bar exists, compute the
    realized forward return per pick and the precision of that day's top longs / shorts."""
    c = _conn(db_path)
    ser = _price_series(c)
    idx = {t: {d: i for i, (d, _) in enumerate(s)} for t, s in ser.items()}

    def fwd(t, as_of, h):
        s = ser.get(t); i = idx.get(t, {}).get(as_of)
        if not s or i is None or i + h >= len(s) or not s[i][1]:
            return None
        return s[i + h][1] / s[i][1] - 1

    out = []
    for snap, as_of, h in c.execute(
            "SELECT DISTINCT snapshot_date, as_of, horizon FROM report_snapshots ORDER BY snapshot_date, horizon"):
        rows = c.execute("SELECT ticker, in_long, in_short FROM report_snapshots "
                         "WHERE snapshot_date=? AND horizon=?", (snap, h)).fetchall()

        def prec(names, side):
            hit = n = 0
            for t in names:
                r = fwd(t, as_of, h)
                if r is None:
                    continue
                n += 1
                if (side == "up" and r > THR) or (side == "down" and r < -THR):
                    hit += 1
            return (round(hit / n, 3) if n else None, n)

        longs = [t for t, l, s in rows if l]
        shorts = [t for t, l, s in rows if s]
        lp, ln = prec(longs, "up"); sp, sn = prec(shorts, "down")
        out.append({"snapshot": snap, "as_of": as_of, "horizon": h,
                    "long_precision": lp, "long_matured": ln, "n_longs": len(longs),
                    "short_precision": sp, "short_matured": sn, "n_shorts": len(shorts),
                    "matured": (ln > 0 or sn > 0)})
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "results": out,
            "n_snapshots": len({r["snapshot"] for r in out}),
            "note": "precision = fraction of the day's picks that actually moved >±3% at that horizon; "
                    "matured rows only. Base rates ~9%/8% (1d) rising with the window."}


def log_view(db_path=DEFAULT_DB) -> dict:
    """Everything logged so far — each snapshot's picks + its realized precision (as it
    matures) — for the /performance page."""
    c = _conn(db_path)
    snaps = []
    for sd, as_of in c.execute("SELECT DISTINCT snapshot_date, as_of FROM report_snapshots "
                               "ORDER BY snapshot_date DESC"):
        longs = [t for (t,) in c.execute(
            "SELECT ticker FROM report_snapshots WHERE snapshot_date=? AND in_long=1 "
            "GROUP BY ticker ORDER BY MIN(rank_up)", (sd,))]
        shorts = [t for (t,) in c.execute(
            "SELECT ticker FROM report_snapshots WHERE snapshot_date=? AND in_short=1 "
            "GROUP BY ticker ORDER BY MIN(rank_down)", (sd,))]
        snaps.append({"snapshot_date": sd, "as_of": as_of, "longs": longs, "shorts": shorts})
    sc = score(db_path)
    return {"snapshots": snaps, "scores": sc["results"], "note": sc["note"]}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    print(json.dumps(snapshot() if cmd == "snapshot" else score(), indent=2, default=str))
