"""health.py — objective, verifiable health of the data collector. No trust required:
every number is read from the DB (heartbeat, per-kind freshness, standing errors, data
recency). Detects STALLS, records them to a persistent SQL log, and fires an alert
(macOS notification + optional email) so you're told the moment collection dies.

  python3 -m health            # print the report
  python3 -m health alert      # check + record + notify on state change  (run from cron)
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import DEFAULT_DB                                       # noqa: E402

HEARTBEAT_STALL_S = 900          # no API call in 15 min => collector STALLED
# kinds that must stay fresh for the pipeline to be trustworthy (transcript is paid/blocked)
CRITICAL = {"bars", "quote", "macro", "implied_move", "analyst", "statements"}


def _conn(db_path):
    c = sqlite3.connect(Path(db_path), timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("""CREATE TABLE IF NOT EXISTS health_alerts(
        ts TEXT NOT NULL, level TEXT, verdict TEXT, message TEXT)""")
    return c


def _age_s(ts, now):
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:                                # date-only or naive -> assume UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds()
    except Exception:
        return None


def health(db_path=DEFAULT_DB) -> dict:
    import collector as C
    c = _conn(db_path)
    now = datetime.now(timezone.utc)
    intervals = {k: v["interval"] for k, v in C.default_collector(db_path).kinds.items()}

    hb = c.execute("SELECT MAX(ts) FROM source_calls").fetchone()[0]
    hb_age = _age_s(hb, now)
    alive = hb_age is not None and hb_age < HEARTBEAT_STALL_S

    kinds, alerts = [], []
    for kind, source, lok, errs, due in c.execute(
            "SELECT kind, source, MAX(last_ok), SUM(last_error IS NOT NULL), "
            "SUM(status='pending' AND next_due<=?) FROM collection_tasks GROUP BY kind",
            (now.isoformat(),)):
        age = _age_s(lok, now)
        iv = intervals.get(kind, 86400)
        # stale = older than 2× its cadence (or 15 min for the fast quote)
        stale = age is None or age > max(2 * iv, 900)
        crit = kind in CRITICAL
        kinds.append({"kind": kind, "source": source, "last_ok": lok,
                      "age_h": round(age / 3600, 1) if age else None,
                      "interval_h": round(iv / 3600, 2), "errors": errs or 0,
                      "due_now": due or 0, "critical": crit, "stale": stale})
        if crit and stale:
            alerts.append(f"{kind} STALE — last collected "
                          f"{round(age/3600,1) if age else '∞'}h ago (cadence {round(iv/3600,1)}h)")

    # data recency — the most recent daily close we hold, and the typed hourly bars
    latest_close = c.execute(
        "SELECT MAX(event_time) FROM feature_values WHERE feature='price.close'").fetchone()[0]
    try:
        import schema
        bars_ts = schema.TypedStore(db_path).c.execute("SELECT MAX(bar_ts) FROM bars").fetchone()[0]
    except Exception:
        bars_ts = None
    # daily bars; allow ~4 days (weekend + Polygon basic-plan EOD delay) before alerting
    _ba = _age_s(bars_ts, now)
    bars_age_d = (_ba / 86400) if _ba is not None else None
    if bars_age_d is not None and bars_age_d > 4:
        alerts.append(f"daily bars STALE — newest {bars_ts[:10]} ({bars_age_d:.0f}d old, "
                      f"beyond weekend+plan-delay)")

    if not alive:
        verdict = "STALLED"
        alerts.insert(0, f"COLLECTOR HEARTBEAT LOST — no API call in "
                         f"{round(hb_age/60) if hb_age else '∞'} min")
    elif alerts:
        verdict = "DEGRADED"
    else:
        verdict = "OK"

    return {"generated_at": now.isoformat(), "verdict": verdict, "alive": alive,
            "heartbeat_age_s": round(hb_age) if hb_age else None, "last_call": hb,
            "kinds": sorted(kinds, key=lambda k: (not k["critical"], -(k["age_h"] or 0))),
            "latest_close": latest_close, "bars_latest": bars_ts,
            "bars_age_d": round(bars_age_d, 1) if bars_age_d else None,
            "alerts": alerts}


def _notify(title, msg):
    try:                                                     # macOS native banner
        subprocess.run(["osascript", "-e",
                        f'display notification {json.dumps(msg)} with title {json.dumps(title)}'],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def alert(db_path=DEFAULT_DB) -> dict:
    """Check health; on a change into DEGRADED/STALLED (or recovery) record it to the SQL
    alert log and fire a notification. Idempotent — only fires on state TRANSITIONS."""
    c = _conn(db_path)
    h = health(db_path)
    prev = c.execute("SELECT verdict FROM health_alerts ORDER BY rowid DESC LIMIT 1").fetchone()
    prev_v = prev[0] if prev else "OK"
    if h["verdict"] != prev_v:
        level = {"OK": "info", "DEGRADED": "warn", "STALLED": "critical"}[h["verdict"]]
        msg = (h["alerts"][0] if h["alerts"] else "collector recovered — all fresh")
        c.execute("INSERT INTO health_alerts VALUES (?,?,?,?)",
                  (h["generated_at"], level, h["verdict"], msg))
        c.commit()
        if h["verdict"] != "OK":
            _notify(f"stock-predictor: collector {h['verdict']}", msg)
    return h


def recent_alerts(db_path=DEFAULT_DB, n=20):
    c = _conn(db_path)
    return [{"ts": ts, "level": lv, "verdict": vd, "message": m} for ts, lv, vd, m in
            c.execute("SELECT ts,level,verdict,message FROM health_alerts "
                      "ORDER BY rowid DESC LIMIT ?", (n,))]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    h = alert() if cmd == "alert" else health()
    print(json.dumps(h, indent=2, default=str))
    sys.exit({"OK": 0, "DEGRADED": 1, "STALLED": 2}[h["verdict"]])
