"""pipeline_map.py — the S1→S2→S3→S4 feature lineage + a live coverage report, for the
/signal-processing dashboard. Encodes the DESIGN contract (what each S2 feature is
derived from and who consumes it downstream) and measures, from feature_values, how much
of it is actually produced. Also names the GAPS: S1 data collected but not yet turned into
a downstream-used S2 feature.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import DEFAULT_DB, RUNTIME_DIR                             # noqa: E402

# ── S2 feature lineage: s2_feature -> (derived-from S1/S2 inputs, downstream consumer) ──
# This is the design contract, hand-authored from the stage code (s2/s3/s4).
LINEAGE = [
    # group, feature(s), derived from, consumed by
    ("technical", ["tech.rsi14", "tech.mom5", "tech.mom20", "tech.hvol20", "tech.vr20",
                   "tech.ret_lag1", "tech.ret_lag2", "tech.ret_lag3", "tech.ret_lag4",
                   "tech.ret_lag5", "tech.ret_lag6", "tech.ret_lag7"],
     "bars → price.close / price.volume (daily EOD)", "S3 model (PREDICTOR_FEATURES)"),
    ("intraday (hourly)", ["tech.intraday_ret", "tech.overnight_gap", "tech.intraday_vol5"],
     "bars → HOURLY OHLCV (within-session granularity)", "available to S3 (validate before adding)"),
    ("long-horizon", ["xh.ret_21d", "xh.ret_63d", "xh.ret_126d", "xh.dist_hi252",
                      "xh.new_high_flag", "xh.above_hi_streak"],
     "bars → price.close (trailing 252d)", "S3 model (champion block)"),
    ("fundamental", ["fund.market_cap", "fund.book_to_price", "fund.earnings_yield",
                     "fund.fcf_yield", "fund.roe", "fund.gross_profitability", "fund.net_margin"],
     "fundamentals (statements) + shares_outstanding + price.close + analyst_snapshot",
     "S3 model (value/quality/size)"),
    ("cross-sectional", ["xsec.rank_rsi14", "xsec.rank_mom5", "xsec.rank_earnings_yield",
                         "xsec.rank_fcf_yield", "xsec.rank_roe", "xsec.rank_gross_profitability"],
     "cross-section of tech.* / fund.* over the universe", "S3 model (selection form)"),
    ("regime", ["regime.breadth5"], "tech.mom5 across the universe", "S4 alpha (regime gate)"),
    ("event calendar", ["calendar.days_to_earnings"],
     "earnings_calendar (earn_date)", "S4 alpha (event risk) — excluded from S3 (PIT)"),
    ("earnings analysis", ["earnings.analysis"],
     "earnings_reports (earn_report) + year-ago quarter", "⚠ produced but UNUSED downstream"),
]

# ── GAPS: S1 collected but not turned into a downstream-used S2 feature ──
# severity: 'missing' (needed by design, absent), 'unused' (collected, no consumer).
GAPS = [
    {"signal": "opt.implied_move", "s1_table": "options_implied", "severity": "missing",
     "issue": "S4 event_risk uses ONLY days_to_earnings; the implied-move magnitude is "
              "collected but ignored — it should sharpen the earnings-proximity risk.",
     "proposed": "S2 event feature: implied_move percentile; feed S4 event_risk."},
    {"signal": "earnings.analysis", "s1_table": "earnings_reports", "severity": "unused",
     "issue": "S2 computes beat/miss, surprise%, revenue YoY — but nothing downstream "
              "reads it; earnings surprise never reaches the model.",
     "proposed": "Scalarize surprise/beat into a PREDICTOR_FEATURE (gated by validation)."},
    {"signal": "short.*", "s1_table": "short_interest", "severity": "unused",
     "issue": "Short interest collected; no S2 feature. (Hurt the daily model in tests → "
              "intended for a near-earnings crowding module that isn't built.)",
     "proposed": "S2 crowding feature for the event module (not the daily model)."},
    {"signal": "insider_transactions", "s1_table": "insider_transactions", "severity": "missing",
     "issue": "Insider buy/sell history collected; no S2 feature (net insider flow).",
     "proposed": "S2 feature: net insider $ flow over 90d, officer-weighted. [backlog]"},
    {"signal": "analyst_revisions", "s1_table": "analyst_revisions", "severity": "missing",
     "issue": "Up/downgrade history collected; no S2 feature (revision drift).",
     "proposed": "S2 feature: net revisions over 90d (up - down). [backlog]"},
    {"signal": "sec_filings / transcripts", "s1_table": "sec_filings", "severity": "missing",
     "issue": "The real earnings report text + call transcripts are collected, but there "
              "is NO S2 NLP layer — the model's 'news-blind' gap remains.",
     "proposed": "S2 NLP: guidance/tone sentiment, surprise-vs-narrative divergence."},
    {"signal": "xbrl_financials", "s1_table": "xbrl_financials", "severity": "missing",
     "issue": "Authoritative full financials now collected, but fund.* still derives from "
              "the thin yfinance 'statements' snapshot.",
     "proposed": "Repoint fund.* to XBRL line items (more complete, PIT via 'filed')."},
]

DOWNSTREAM_INPUTS = {                                                # for the contract panel
    "S3 model (PREDICTOR_FEATURES)": [
        "tech.rsi14", "tech.mom5", "tech.mom20", "tech.hvol20", "tech.vr20",
        "tech.ret_lag1", "tech.ret_lag2", "tech.ret_lag3", "tech.ret_lag4",
        "tech.ret_lag5", "tech.ret_lag6", "tech.ret_lag7",
        "fund.book_to_price", "fund.earnings_yield", "fund.fcf_yield", "fund.roe",
        "fund.gross_profitability", "fund.net_margin", "fund.market_cap",
        "xsec.rank_rsi14", "xsec.rank_mom5", "xsec.rank_earnings_yield",
        "xsec.rank_fcf_yield", "xsec.rank_roe", "xsec.rank_gross_profitability",
        "xh.ret_21d", "xh.ret_63d", "xh.ret_126d", "xh.dist_hi252",
        "xh.new_high_flag", "xh.above_hi_streak"],
    "S4 alpha": ["regime.breadth5", "macro.vix", "macro.spy_close",
                 "calendar.days_to_earnings", "predict.ret_1d"],
}


def _hours_since(ts, now):
    if not ts:
        return None
    try:
        return round((now - datetime.fromisoformat(ts)).total_seconds() / 3600, 1)
    except Exception:
        return None


def report(db_path=DEFAULT_DB) -> dict:
    """Live coverage of every S2/downstream feature from feature_values, plus the
    lineage and the gap list, for the /signal-processing dashboard."""
    c = sqlite3.connect(Path(db_path))
    now = datetime.now(timezone.utc)

    def cov(feature):
        r = c.execute("SELECT COUNT(*), COUNT(DISTINCT scope), COUNT(DISTINCT event_time), "
                      "MIN(event_time), MAX(event_time), MAX(ingested_at) "
                      "FROM feature_values WHERE feature=?", (feature,)).fetchone()
        rows, scopes, ndates, first, latest, ing = r
        return {"feature": feature, "rows": rows or 0, "scopes": scopes or 0,
                "n_dates": ndates or 0, "first": first, "latest": latest,
                "fresh_h": _hours_since(ing, now)}

    groups = []
    for group, feats, derived, consumer in LINEAGE:
        items = [cov(f) for f in feats]
        groups.append({"group": group, "derived_from": derived, "consumer": consumer,
                       "features": items,
                       "scopes_max": max((i["scopes"] for i in items), default=0),
                       "dates_max": max((i["n_dates"] for i in items), default=0),
                       "first": min((i["first"] or "9999" for i in items), default=""),
                       "latest": max((i["latest"] or "" for i in items), default="")})

    # downstream contract: is each required input actually produced?
    contract = {}
    for consumer, feats in DOWNSTREAM_INPUTS.items():
        contract[consumer] = [{**cov(f), "produced": cov(f)["scopes"] > 0} for f in feats]

    # gap enrichment: how many tickers hold the underlying S1 signal (so the gap is
    # 'data is here, feature is missing', not 'no data')
    def s1_entities(table):
        try:
            ent = "name" if table == "macro" else "ticker"
            return c.execute(f"SELECT COUNT(DISTINCT {ent}) FROM {table}").fetchone()[0]
        except Exception:
            return 0
    gaps = [{**g, "s1_tickers": s1_entities(g["s1_table"])} for g in GAPS]

    return {"generated_at": now.isoformat(), "groups": groups,
            "contract": contract, "gaps": gaps,
            "s2_feature_count": sum(len(g["features"]) for g in groups)}


# ── the dataflow DAG: nodes (signals) + dependency edges, per stage ──
# S1 collected signals (id, kind). kind drives the click-view: line|raw|json.
S1_SIGNALS = [
    ("bars", "line"),                                   # HOURLY OHLCV (the raw intraday price)
    ("price.close", "line"), ("price.volume", "line"), ("price.current", "raw"),
    ("macro.vix", "line"), ("macro.spy_close", "line"), ("macro.yield10y", "line"),
    ("short.pct_float", "line"), ("opt.implied_move", "line"),
    ("earnings.report_raw", "raw"), ("earnings.next_date", "json"),
    ("fundamental.statements", "raw"), ("fundamental.shares_outstanding", "line"),
    ("fundamental.analyst_snapshot", "json"),
    ("analyst.revisions_raw", "raw"), ("insider.transactions_raw", "raw"),
    ("sec_filings", "raw"), ("xbrl_financials", "raw"), ("transcripts", "raw"),
]
# feature/node -> typed table for the raw view
RAW_TABLE = {
    "earnings.report_raw": "earnings_reports", "analyst.revisions_raw": "analyst_revisions",
    "insider.transactions_raw": "insider_transactions", "fundamental.statements": "fundamentals",
    "price.current": "quotes", "sec_filings": "sec_filings",
    "xbrl_financials": "xbrl_financials", "transcripts": "transcripts",
}
# S3 nodes: the FUTURE prediction (predict.*, one point at the latest date) and the
# BACKTEST evaluation series (backtest.*, historical walk-forward — for measuring skill).
_S3_PRED = ["predict.ret_1d", "predict.ret_5d", "predict.ret_21d", "predict.vol_5d"]
_S3_BACKTEST = ["backtest.ret_1d", "backtest.ret_5d", "backtest.ret_21d", "backtest.vol_5d"]


def _depends():
    """downstream signal -> [upstream signals]. Built once, uses S3's real vector."""
    import s3_predictors
    d = {}
    for x in ["rsi14", "mom5", "mom20", "hvol20", "vr20", "ret_lag1", "ret_lag2",
              "ret_lag3", "ret_lag4", "ret_lag5", "ret_lag6", "ret_lag7"]:
        d[f"tech.{x}"] = ["price.close", "price.volume"]
    for x in ["intraday_ret", "overnight_gap", "intraday_vol5"]:
        d[f"tech.{x}"] = ["bars"]                        # intraday features use the HOURLY bars
    d["price.close"] = ["bars"]; d["price.volume"] = ["bars"]   # daily EOD projection of hourly
    for x in ["ret_21d", "ret_63d", "ret_126d", "dist_hi252", "new_high_flag", "above_hi_streak"]:
        d[f"xh.{x}"] = ["price.close"]
    for x in ["market_cap", "book_to_price", "earnings_yield", "fcf_yield"]:
        d[f"fund.{x}"] = ["fundamental.statements", "price.close"]
    for x in ["roe", "gross_profitability", "net_margin"]:
        d[f"fund.{x}"] = ["fundamental.statements"]
    d["xsec.rank_rsi14"] = ["tech.rsi14"]; d["xsec.rank_mom5"] = ["tech.mom5"]
    d["xsec.rank_earnings_yield"] = ["fund.earnings_yield"]
    d["xsec.rank_fcf_yield"] = ["fund.fcf_yield"]
    d["xsec.rank_roe"] = ["fund.roe"]
    d["xsec.rank_gross_profitability"] = ["fund.gross_profitability"]
    d["regime.breadth5"] = ["tech.mom5"]
    d["calendar.days_to_earnings"] = ["earnings.next_date"]
    d["earnings.analysis"] = ["earnings.report_raw"]
    pf = list(s3_predictors.PREDICTOR_FEATURES)
    for p in _S3_PRED + _S3_BACKTEST:               # every predictor consumes the S2 vector
        d[p] = pf
    d["predict.forecast"] = ["predict.ret_1d", "predict.ret_5d", "predict.ret_21d"]  # future rollup
    d["alpha.regime"] = ["regime.breadth5", "macro.vix", "macro.spy_close"]
    d["alpha.event_risk"] = ["calendar.days_to_earnings"]
    d["alpha.signal"] = ["predict.ret_1d", "alpha.regime", "alpha.event_risk"]
    return d


def stock_graph(ticker: str, db_path=DEFAULT_DB) -> dict:
    """The per-stock dataflow DAG: every signal as a node (stage + kind + produced?),
    and dependency edges upstream→downstream."""
    ticker = (ticker or "").upper()
    c = sqlite3.connect(Path(db_path))
    import schema
    ts = schema.TypedStore(db_path)

    # coverage for feature_values features (ndates + latest), deduped by scope
    fv = {}
    for feat, nd, latest in c.execute(
            "SELECT feature, COUNT(DISTINCT event_time), MAX(event_time) FROM feature_values "
            "WHERE scope IN (?, '_market') GROUP BY feature", (ticker,)):
        fv[feat] = (nd, latest)

    def typed_count(table):
        try:
            ent = "ticker"
            return ts.c.execute(f"SELECT COUNT(*) FROM {table} WHERE {ent}=?",
                                (ticker,)).fetchone()[0]
        except Exception:
            return 0

    deps = _depends()
    # assemble node defs across stages
    defs = [(f, "S1", k) for f, k in S1_SIGNALS]
    for group, feats, _, _ in LINEAGE:
        for f in feats:
            defs.append((f, "S2", "json" if f == "earnings.analysis" else "line"))
    defs += ([("predict.forecast", "S3", "json")]
             + [(f, "S3", "line") for f in _S3_PRED]
             + [(f, "S3", "line") for f in _S3_BACKTEST])
    defs += [("alpha.regime", "S4", "json"), ("alpha.event_risk", "S4", "json"),
             ("alpha.signal", "S4", "json")]

    seen, nodes = set(), []
    for fid, stage, kind in defs:
        if fid in seen:
            continue
        seen.add(fid)
        if fid == "bars":                                     # hourly bars (typed-only, line)
            nd, latest = typed_count("bars"), None
        elif fid in RAW_TABLE and fid not in fv:              # typed-only S1 (sec_filings…)
            n = typed_count(RAW_TABLE[fid]); nd, latest = n, None
        else:
            nd, latest = fv.get(fid, (0, None))
        label = (("bt·" + fid.split(".", 1)[1]) if fid.startswith("backtest.")
                 else (fid.split(".", 1)[-1] if "." in fid else fid))
        nodes.append({"id": fid, "stage": stage, "kind": kind,
                      "produced": (nd or 0) > 0, "n": nd or 0, "latest": latest, "label": label})
    nodeset = {n["id"] for n in nodes}
    edges = [[u, v] for v, us in deps.items() for u in us if u in nodeset and v in nodeset]
    latest = fv.get("price.close", (0, None))[1]
    return {"ticker": ticker, "nodes": nodes, "edges": edges,
            "stages": ["S1", "S2", "S3", "S4"], "latest": latest}


def stock_signal(ticker: str, feature: str, db_path=DEFAULT_DB) -> dict:
    """The best view of ONE signal for a stock: a numeric time series (line), the raw
    typed rows (raw), or the latest structured value (json)."""
    import json
    ticker = (ticker or "").upper()
    c = sqlite3.connect(Path(db_path))
    if feature == "bars":                                     # HOURLY price — line at 1h granularity
        import schema
        rows = schema.TypedStore(db_path).c.execute(
            "SELECT bar_ts, close FROM bars WHERE ticker=? ORDER BY bar_ts", (ticker,)).fetchall()
        pts = [[ts[:16].replace("T", " "), cl] for ts, cl in rows if cl is not None]
        return {"kind": "line", "feature": "bars · hourly close", "points": pts,
                "latest": pts[-1][1] if pts else None, "n": len(pts)}
    if feature in RAW_TABLE:                                   # raw document / event list
        import schema
        r = schema.TypedStore(db_path).rows(RAW_TABLE[feature], ticker, limit=40)
        for row in r["rows"]:
            for k, v in row.items():
                if isinstance(v, str) and len(v) > 200:
                    row[k] = v[:200] + f"… ({len(v)} chars)"
        return {"kind": "raw", "feature": feature, "table": r["table"],
                "columns": r["columns"], "ts_col": r["ts_col"], "rows": r["rows"]}
    # time series from feature_values (dedup to latest ingested_at per event_time)
    rows = c.execute(
        "SELECT event_time, value, MAX(ingested_at) FROM feature_values "
        "WHERE feature=? AND scope IN (?, '_market') GROUP BY event_time ORDER BY event_time",
        (feature, ticker)).fetchall()
    pts, nonnum = [], None
    for et, val, _ in rows:
        try:
            pts.append([et[:10], float(val)])
        except (TypeError, ValueError):
            nonnum = val                                      # JSON/text -> not a line
    if pts and nonnum is None:
        out = {"kind": "line", "feature": feature, "points": pts,
               "latest": pts[-1][1], "n": len(pts)}
        if feature.startswith(("predict.", "backtest.")):   # mark the training/OOS trust boundary
            try:
                out["train_end"] = json.loads(
                    (RUNTIME_DIR / "production_model.json").read_text()).get("train_end")
            except Exception:
                pass
        if feature.startswith(("predict.ret_", "backtest.ret_")):  # per-point 95% PI half-width
            cif = feature.replace(".ret_", ".ci_ret_")
            ciby = {}
            for et, val, _ in c.execute(
                    "SELECT event_time, value, MAX(ingested_at) FROM feature_values "
                    "WHERE feature=? AND scope=? GROUP BY event_time", (cif, ticker)):
                try:
                    ciby[et[:10]] = float(val)
                except (TypeError, ValueError):
                    pass
            if ciby:
                out["ciseries"] = [ciby.get(d) for d, _ in pts]
        return out
    # structured / json latest
    latest = rows[-1] if rows else None
    v = latest[1] if latest else None
    if isinstance(v, str) and v[:1] in "{[":
        try:
            v = json.loads(v)
        except Exception:
            pass
    return {"kind": "json", "feature": feature,
            "event_time": latest[0] if latest else None, "value": v}


def predictor_report(db_path=DEFAULT_DB) -> dict:
    """One row per predictor model: family, horizon, coverage, OOS skill (return IC +
    directional hit-rate; up/down hit-rates; vol correlation), CI width, and whether it's
    the promoted production model. Skill is measured on the walk-forward OOS predictions
    vs. realized forward outcomes — the number you watch before promoting."""
    import json
    import math
    from s3_multi import HORIZONS, VOL_HORIZON
    con = sqlite3.connect(Path(db_path))
    price = {}
    for sc, et, val in con.execute(
            "SELECT scope, event_time, value FROM feature_values WHERE feature='price.close'"):
        try:
            price.setdefault(sc, {})[et] = float(val)
        except (TypeError, ValueError):
            pass

    def series(feat):
        out = {}
        for sc, et, val, _ in con.execute(
                "SELECT scope, event_time, value, MAX(ingested_at) FROM feature_values "
                "WHERE feature=? GROUP BY scope, event_time", (feat,)):
            try:
                out[(sc, et)] = float(val)
            except (TypeError, ValueError):
                pass
        return out

    def realized_ret(h):
        r = {}
        for t, pm in price.items():
            ds = sorted(pm); cl = [pm[d] for d in ds]
            for i, d in enumerate(ds):
                if i + h < len(cl) and cl[i]:
                    r[(t, d)] = cl[i + h] / cl[i] - 1
        return r

    def realized_vol(h):
        r = {}
        for t, pm in price.items():
            ds = sorted(pm); cl = [pm[d] for d in ds]
            dr = [cl[i + 1] / cl[i] - 1 for i in range(len(cl) - 1) if cl[i]]
            for i, d in enumerate(ds):
                w = dr[i:i + h]
                if len(w) == h:
                    m = sum(w) / h
                    r[(t, d)] = math.sqrt(sum((x - m) ** 2 for x in w) / h)
        return r

    def pearson(xs, ys):
        n = len(xs)
        if n < 3:
            return None
        mx = sum(xs) / n; my = sum(ys) / n
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        dx = math.sqrt(sum((a - mx) ** 2 for a in xs)); dy = math.sqrt(sum((b - my) ** 2 for b in ys))
        return num / (dx * dy) if dx and dy else None

    band = {}
    row = con.execute("SELECT value FROM feature_values WHERE feature='predict.band' "
                      "ORDER BY ingested_at DESC LIMIT 1").fetchone()
    if row:
        try:
            band = json.loads(row[0])
        except Exception:
            pass
    prod = {}
    try:
        prod = json.loads((RUNTIME_DIR / "production_model.json").read_text())
    except Exception:
        pass
    promoted = set(prod.get("predictors", []))

    # skill is trusted only OUT-OF-SAMPLE — dates strictly after the production model's
    # train_end (predictions on training dates overlap what the model saw).
    train_end = prod.get("train_end")

    def oos(k):
        return (not train_end) or k[1] > train_end

    # skill is measured on the BACKTEST (historical walk-forward), OOS-only.
    predictors = []
    for h in HORIZONS:
        rr = realized_ret(h)
        ps = series(f"backtest.ret_{h}d")
        pr = [(v, rr[k]) for k, v in ps.items() if k in rr and oos(k)]
        ic = pearson([a for a, _ in pr], [b for _, b in pr]) if pr else None
        hit = (sum(1 for a, b in pr if (a > 0) == (b > 0)) / len(pr)) if pr else None
        predictors.append({
            "feature": f"ret_{h}d", "family": "return", "horizon": f"{h}d",
            "model": "Ridge", "n": len(ps), "n_oos": len(pr), "tickers": len({k[0] for k in ps}),
            "metric": "IC / hit", "ic": ic, "hit": hit,
            "ci95": round(1.96 * band[f"ret_{h}d"], 4) if f"ret_{h}d" in band else None,
            "promoted": f"ret_{h}d" in promoted})
        up, dn = series(f"backtest.up_{h}d"), series(f"backtest.down_{h}d")
        up_sel = [(v, rr[k]) for k, v in up.items() if k in rr and oos(k) and v > 0.5]
        dn_sel = [(v, rr[k]) for k, v in dn.items() if k in rr and oos(k) and v > 0.5]
        predictors.append({
            "feature": f"up_{h}d / down_{h}d", "family": "direction", "horizon": f"{h}d",
            "model": "derived: Φ(ŷ/se)", "n": len(up), "n_oos": len(up_sel),
            "tickers": len({k[0] for k in up}), "metric": "up-hit / down-hit",
            "up_hit": (sum(1 for _, r in up_sel if r > 0) / len(up_sel)) if up_sel else None,
            "down_hit": (sum(1 for _, r in dn_sel if r < 0) / len(dn_sel)) if dn_sel else None,
            "promoted": f"ret_{h}d" in promoted})           # direction derives from the return model
    rv = realized_vol(VOL_HORIZON)
    vs = series(f"backtest.vol_{VOL_HORIZON}d")
    vp = [(v, rv[k]) for k, v in vs.items() if k in rv and oos(k)]
    predictors.append({
        "feature": f"vol_{VOL_HORIZON}d", "family": "volatility", "horizon": f"{VOL_HORIZON}d",
        "model": "Ridge", "n": len(vs), "n_oos": len(vp), "tickers": len({k[0] for k in vs}),
        "metric": "corr(realized)",
        "vol_corr": pearson([a for a, _ in vp], [b for _, b in vp]) if vp else None,
        "promoted": f"vol_{VOL_HORIZON}d" in promoted})

    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "predictors": predictors, "production": prod, "train_end": train_end,
            "n_promoted": len(promoted), "band": band}


def _step_of(feature: str) -> str:
    if feature.startswith("alpha."):
        return "S4 · Alpha"
    if feature.startswith(("predict.", "backtest.")):
        return "S3 · Predictors"
    if (feature.startswith(("tech.", "fund.", "xsec.", "xh.", "regime."))
            or feature in ("calendar.days_to_earnings", "earnings.analysis",
                           "fundamental.earnings_signal")):
        return "S2 · Signals"
    return "S1 · Raw"


STEP_ORDER = ["S1 · Raw", "S2 · Signals", "S3 · Predictors", "S4 · Alpha"]


def _fmt(v):
    """Compact display for a stored value (float/int/dict/long text)."""
    import json
    if isinstance(v, str) and v[:1] in "{[":
        try:
            v = json.loads(v)
        except Exception:
            pass
    if isinstance(v, dict):
        return ", ".join(f"{k}={_fmt(val)}" for k, val in list(v.items())[:8])
    if isinstance(v, float):
        return f"{v:,.4g}"
    if isinstance(v, str) and len(v) > 120:
        return v[:120] + f"… ({len(v)} chars)"
    return str(v)


def single_stock(ticker: str, db_path=DEFAULT_DB) -> dict:
    """Everything the pipeline holds for ONE stock: every signal at every step (S1→S4)
    with its latest value + time-series depth, plus the FULL raw S1 rows underneath."""
    import schema
    ticker = (ticker or "").upper()
    c = sqlite3.connect(Path(db_path))
    now = datetime.now(timezone.utc)

    # latest value per feature for this ticker (and the market context it rides on)
    latest = c.execute(
        "SELECT fv.feature, fv.event_time, fv.value, fv.ingested_at "
        "FROM feature_values fv JOIN (SELECT feature, scope, MAX(event_time) me "
        "  FROM feature_values WHERE scope IN (?, '_market') GROUP BY feature, scope) m "
        "ON fv.feature=m.feature AND fv.scope=m.scope AND fv.event_time=m.me "
        "WHERE fv.scope IN (?, '_market')", (ticker, ticker)).fetchall()
    ndates = {f: n for f, n in c.execute(
        "SELECT feature, COUNT(DISTINCT event_time) FROM feature_values "
        "WHERE scope IN (?, '_market') GROUP BY feature", (ticker,))}

    # dedup per feature: a value can have several ingested_at at the same event_time
    # (bitemporal re-collection) — keep the newest (event_time, then ingested_at).
    best = {}
    for feature, et, value, ing in latest:
        cur = best.get(feature)
        if cur is None or (et or "", ing or "") > (cur[1] or "", cur[3] or ""):
            best[feature] = (feature, et, value, ing)

    steps = {s: [] for s in STEP_ORDER}
    for feature, et, value, ing in best.values():
        steps[_step_of(feature)].append({
            "feature": feature, "value": _fmt(value), "event_time": et,
            "n_dates": ndates.get(feature, 0), "fresh_h": _hours_since(ing, now)})
    for s in steps:
        steps[s].sort(key=lambda x: x["feature"])

    # FULL raw S1 rows from the typed tables (the actual collected data)
    ts = schema.TypedStore(db_path)
    raw = []
    for table in schema.SCHEMA:
        if table == "macro":
            continue                                        # market-wide, not per-stock
        r = ts.rows(table, ticker, limit=6)
        if r["rows"]:
            for row in r["rows"]:                           # truncate long text columns
                for k, v in row.items():
                    if isinstance(v, str) and len(v) > 90:
                        row[k] = v[:90] + f"… ({len(v)})"
            raw.append(r)

    return {"generated_at": now.isoformat(), "ticker": ticker, "steps": steps,
            "step_order": STEP_ORDER, "raw": raw,
            "signal_count": sum(len(v) for v in steps.values())}
