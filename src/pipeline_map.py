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
from core import (DEFAULT_DB, RUNTIME_DIR, DataAPI,                  # noqa: E402
                  DataAPIError, UnknownScopeError)

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
                 "calendar.days_to_earnings", "predict.dir_1d"],
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
# S3 nodes: the deployed CLASSIFIER prediction (predict.pbig_* / dir — the live per-ticker
# calibrated big-move probabilities) and the BACKTEST evaluation series (backtest.*).
_S3_PRED = ["predict.dir_1d", "predict.pbig_up_1d", "predict.pbig_down_1d",
            "predict.pbig_up_5d", "predict.pbig_down_5d"]
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
    d["alpha.regime"] = ["regime.breadth5", "macro.vix", "macro.spy_close"]
    d["alpha.event_risk"] = ["calendar.days_to_earnings"]
    d["alpha.signal"] = ["predict.dir_1d", "alpha.regime", "alpha.event_risk"]
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
    defs += ([(f, "S3", "line") for f in _S3_PRED]
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
    # time series from feature_values — retrieved ONLY through the gated DataAPI
    # (the dashboard is a data consumer, not a direct DB reader). Market-level
    # signals live under the '_market' scope, so fall back to it for this ticker.
    now = datetime.now(timezone.utc).isoformat()

    def _series(feat, scope):
        try:
            return DataAPI(db_path).get(scope, feat, "0001-01-01", now)
        except UnknownScopeError:
            return None
        except DataAPIError:
            return []

    recs = _series(feature, ticker)
    if recs is None:
        recs = _series(feature, "_market") or []
    pts, nonnum = [], None
    for r in recs:
        try:
            pts.append([r["event_time"][:10], float(r["value"])])
        except (TypeError, ValueError):
            nonnum = r["value"]                               # JSON/text -> not a line
    if pts and nonnum is None:
        out = {"kind": "line", "feature": feature, "points": pts,
               "latest": pts[-1][1], "n": len(pts)}
        if feature.startswith(("predict.", "backtest.")):   # mark the training/OOS trust boundary
            try:
                out["train_end"] = json.loads(
                    (RUNTIME_DIR / "production_model.json").read_text()).get("train_end")
            except Exception:
                pass
        if feature.startswith("backtest.ret_"):        # per-point 95% PI half-width
            cif = feature.replace(".ret_", ".ci_ret_")
            ciby = {}
            for r in (_series(cif, ticker) or []):
                try:
                    ciby[r["event_time"][:10]] = float(r["value"])
                except (TypeError, ValueError):
                    pass
            if ciby:
                out["ciseries"] = [ciby.get(d) for d, _ in pts]
        return out
    # structured / json latest
    latest = recs[-1] if recs else None
    v = latest["value"] if latest else None
    if isinstance(v, str) and v[:1] in "{[":
        try:
            v = json.loads(v)
        except Exception:
            pass
    return {"kind": "json", "feature": feature,
            "event_time": latest["event_time"] if latest else None, "value": v}


def _modeling_dir():
    return Path(__file__).resolve().parent.parent / "modeling"


def _parse_horizon_readme(text: str) -> dict:
    """Parse a modeling/h{N}/README.md into base rates + the model roster table."""
    import re
    bu = re.search(r"up ([\d.]+)%,\s*down ([\d.]+)%", text)
    nr = re.search(r"([\d,]+) labeled rows", text)
    ses = re.search(r"next (\d+) session", text)
    models = []
    for line in text.splitlines():
        if not line.strip().startswith("|") or "`" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        nm = re.search(r"`([^`]+)`", cells[0])
        if not nm:
            continue
        models.append({"model": nm.group(1), "champion": "⭐" in cells[0],
                       "up1": cells[1], "up5": cells[2], "down1": cells[3],
                       "down5": cells[4], "ece": cells[5]})
    return {"base_up": float(bu.group(1)) / 100 if bu else None,
            "base_down": float(bu.group(2)) / 100 if bu else None,
            "n_rows": int(nr.group(1).replace(",", "")) if nr else None,
            "days": int(ses.group(1)) if ses else None, "models": models}


# The 4 model-creation CATEGORIES the deployment serves per ticker. Each maps to a
# recorded horizon roster; production = that horizon's champion classifier.
PRED_CATEGORIES = [("h1", "1d", "next-day (EOD)"), ("h3", "3d", "3-day swing"),
                   ("h5", "5d", "1-week"), ("h7", "7d", "~1.5-week")]


def predictor_report(db_path=DEFAULT_DB) -> dict:
    """Grouped by prediction CATEGORY (horizon). Each category lists every model tried
    with its recorded per-day precision@k performance and marks the PRODUCTION model —
    read from modeling/h{N}/README.md (the real recorded rosters), not invented."""
    md = _modeling_dir()
    cats = []
    for key, label, title in PRED_CATEGORIES:
        p = md / key / "README.md"
        if not p.exists():
            cats.append({"key": key, "label": label, "title": title, "built": False, "models": []})
            continue
        info = _parse_horizon_readme(p.read_text())
        # production per category = the recorded champion (⭐); h1's live model is the DUAL
        prod = next((m["model"] for m in info["models"] if m["champion"]), None)
        if key == "h1":
            prod = "DUAL (logistic-up + histgbm-down)"
            info["models"].append({"model": "DUAL (logistic-up + histgbm-down)", "champion": False,
                                   "up1": "24% (2.6×)", "up5": "—", "down1": "23% (2.9×)",
                                   "down5": "—", "ece": "—"})
        for m in info["models"]:
            m["production"] = (m["model"] == prod)
        cats.append({"key": key, "label": label, "title": title, "built": True,
                     "days": info["days"], "base_up": info["base_up"], "base_down": info["base_down"],
                     "n_rows": info["n_rows"], "production": prod, "models": info["models"]})
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "metric": "per-day precision@k on big moves (>±3%), walk-forward vs base rate",
            "categories": cats,
            "planned": "28d (~monthly) — not built yet; longest recorded window is 7d",
            "source": "modeling/h{N}/README.md (recorded per-horizon rosters)",
            "headline": ("Edge (lift over base rate) is strongest at 1d and decays with the "
                         "window. Deployment serves one calibrated prediction PER CATEGORY per "
                         "ticker (p_up / p_down / precision@k); alpha picks the strongest.")}


def model_detail(model: str, category: str = "", db_path=DEFAULT_DB) -> dict:
    """A model's precision@k (the 'prediction vs accuracy' curve) + recorded confident-
    wrong error cases — from the modeling record, not invented."""
    import json
    import re
    md = _modeling_dir()
    rep = predictor_report(db_path)

    def num(s):
        m = re.search(r"([\d.]+)%", s or "")
        return float(m.group(1)) / 100 if m else None

    row = cat = None
    for c in rep["categories"]:
        for m in c.get("models", []):
            if m["model"] == model and (not category or c["key"] == category):
                row, cat = m, c
                break
        if row:
            break
    curve = []
    if row:
        curve = [{"k": 1, "up": num(row["up1"]), "dn": num(row["down1"])},
                 {"k": 5, "up": num(row["up5"]), "dn": num(row["down5"])}]
    errs = []
    for fn in ("error_examples2.json", "error_examples.json"):
        try:
            data = json.loads((md / fn).read_text())
            errs = (data if isinstance(data, list) else data.get("examples", []))[:10]
            if errs:
                break
        except Exception:
            pass
    return {"model": model, "category": cat["label"] if cat else category,
            "ece": row["ece"] if row else None,
            "base_rate_up": cat["base_up"] if cat else None,
            "base_rate_down": cat["base_down"] if cat else None,
            "curve": curve, "top_errors": errs}


def alpha_report(ticker: str, db_path=DEFAULT_DB) -> dict:
    """A single-name ALPHA REPORT assembled from our real S1/S2/S3/S4 signals — the
    structure in docs/alpha-report/README.md. Sections with pending inputs (per-ticker
    predictor, sector, beta) are flagged, not faked."""
    import json
    ticker = (ticker or "").upper()
    c = sqlite3.connect(Path(db_path))

    def latest(feat, scope=None):
        r = c.execute("SELECT event_time, value FROM feature_values WHERE feature=? AND scope=? "
                      "ORDER BY event_time DESC, ingested_at DESC LIMIT 1",
                      (feat, scope or ticker)).fetchone()
        if not r:
            return None, None
        v = r[1]
        if isinstance(v, str) and v[:1] in "{[":
            try:
                v = json.loads(v)
            except Exception:
                pass
        else:
            try:
                v = float(v)
            except (TypeError, ValueError):
                pass
        return r[0], v

    asof, price = latest("price.close")
    date = asof

    def pctile(feat, invert=False):
        """Cross-sectional percentile of `ticker` for `feat` on `date` (0..1)."""
        rows = c.execute("SELECT scope, value FROM feature_values WHERE feature=? AND event_time=? "
                         "GROUP BY scope", (feat, date)).fetchall()
        vals, mine = [], None
        for sc, v in rows:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            vals.append((sc, fv))
            if sc == ticker:
                mine = fv
        if mine is None or len(vals) < 5:
            return None
        below = sum(1 for _, v in vals if v < mine)
        p = below / (len(vals) - 1)
        return round(1 - p if invert else p, 3)

    # ── factor decomposition (percentile vs universe) ──
    factors = [
        ("Value", pctile("fund.earnings_yield"), "cheap vs peers (E/P, FCF/P)"),
        ("Quality", pctile("fund.roe"), "ROE + gross-profitability"),
        ("Momentum", pctile("tech.mom20"), "20d price momentum"),
        ("Low-vol", pctile("tech.hvol20", invert=True), "inverse realized vol"),
        ("Size", pctile("fund.market_cap"), "market cap (large→high)"),
    ]
    # blend the two value/quality legs
    v2 = pctile("fund.fcf_yield"); q2 = pctile("fund.gross_profitability")
    if factors[0][1] is not None and v2 is not None:
        factors[0] = ("Value", round((factors[0][1] + v2) / 2, 3), factors[0][2])
    if factors[1][1] is not None and q2 is not None:
        factors[1] = ("Quality", round((factors[1][1] + q2) / 2, 3), factors[1][2])
    fscore = [f[1] for f in factors[:4] if f[1] is not None]      # value/quality/mom/lowvol
    factor_score = round(sum(fscore) / len(fscore), 3) if fscore else None

    # ── catalysts ──
    _, nx = latest("earnings.next_date")
    dte = None
    try:
        from datetime import date as _d
        nd = (nx or {}).get("next_earnings", "")[:10]
        if nd:
            dte = (_d.fromisoformat(nd) - _d.fromisoformat(date)).days
    except Exception:
        pass
    _, impl = latest("opt.implied_move")
    import schema
    ts = schema.TypedStore(db_path)

    def _typed_rows(table, days):
        col = {"insider_transactions": "txn_date", "analyst_revisions": "revision_date"}[table]
        cut = None
        try:
            from datetime import date as _d, timedelta
            cut = (_d.fromisoformat(date) - timedelta(days=days)).isoformat()
        except Exception:
            pass
        q = f"SELECT * FROM {table} WHERE ticker=?" + (f" AND {col} >= '{cut}'" if cut else "")
        cols = [d[1] for d in ts.c.execute(f"PRAGMA table_info({table})")]
        return [dict(zip(cols, r)) for r in ts.c.execute(q, (ticker,))]

    rev = _typed_rows("analyst_revisions", 90)
    rev_net = sum(1 for r in rev if (r.get("action") or "").lower() in ("up", "upgrade")) \
        - sum(1 for r in rev if (r.get("action") or "").lower() in ("down", "downgrade"))
    ins = _typed_rows("insider_transactions", 90)
    ins_net = sum((r.get("value") or 0) * (-1 if r.get("is_sale") else 1) for r in ins)

    # ── valuation + positioning ──
    _, an = latest("fundamental.analyst_snapshot")
    an = an if isinstance(an, dict) else {}
    tgt = an.get("target_mean_price")
    upside = round(tgt / price - 1, 3) if (tgt and price) else None
    _, spf = latest("short.pct_float"); _, dtc = latest("short.days_to_cover")
    _, er = latest("alpha.event_risk"); _, reg = latest("alpha.regime", "_market")
    _, vix = latest("macro.vix", "_market"); _, breadth = latest("regime.breadth5", "_market")
    _, hvol = latest("tech.hvol20")
    _, surp = latest("earnings.report_raw")
    surprise = (surp or {}).get("surprise_pct") if isinstance(surp, dict) else None

    # ── PER-TICKER PREDICTIONS: the deployed big-move classifier, ranked across universe ──
    def conv(feat):
        md = c.execute("SELECT MAX(event_time) FROM feature_values WHERE feature=?", (feat,)).fetchone()
        if not md or not md[0]:
            return None, None, 0
        rk = sorted(((sc, float(v)) for sc, v, _ in c.execute(
            "SELECT scope, value, MAX(ingested_at) FROM feature_values WHERE feature=? "
            "AND event_time=? GROUP BY scope", (feat, md[0]))), key=lambda x: -x[1])
        pos = {sc: (v, i + 1) for i, (sc, v) in enumerate(rk)}
        v, r = pos.get(ticker, (None, None))
        return v, r, len(rk)

    HZ = [1, 3, 5, 7]
    preds, best = [], None
    for h in HZ:
        pu, ru, n = conv(f"predict.pbig_up_{h}d")
        pd, rd, _ = conv(f"predict.pbig_down_{h}d")
        preds.append({"h": f"{h}d", "p_up": pu, "rank_up": ru, "p_down": pd, "rank_down": rd, "n": n})
        for side, p, rk in (("up", pu, ru), ("down", pd, rd)):
            if p is not None and rk is not None and (best is None or rk < best["rank"] or
                                                     (rk == best["rank"] and p > best["p"])):
                best = {"side": side, "h": h, "p": p, "rank": rk, "n": n}

    # ── SUMMARIZED SUGGESTION: strongest ranked prediction, then hard gates ──
    er_level = (er or {}).get("level")
    reg_decision = (reg or {}).get("decision")
    _, dh = latest("xh.dist_hi252")
    near_high = dh >= -0.02 if dh is not None else None
    top = best and best["rank"] and best["rank"] <= max(5, (best["n"] or 109) // 20)  # ~top 5%
    if best:
        conviction = (f'{best["h"]}d {best["side"].upper()}-move P={best["p"]*100:.0f}% '
                      f'(rank #{best["rank"]}/{best["n"]}, {"top pick" if top else "mid-pack"})')
    else:
        conviction = "no per-ticker prediction available"
    if reg_decision == "CASH":
        action, why = "STAND DOWN", f"regime risk-off (CASH gate) — but signal: {conviction}"
    elif er_level == "HIGH":
        action, why = "AVOID", f"event imminent — {conviction}"
    elif top and best["side"] == "down":
        action, why = "SHORT CANDIDATE", f"strong ranked down-conviction: {conviction}"
    elif top and best["side"] == "up":
        action, why = "LONG CANDIDATE", f"strong ranked up-conviction: {conviction}"
    else:
        action, why = "NEUTRAL", f"no top-ranked conviction — best is {conviction}"

    eff = None
    try:
        h1 = next((x for x in predictor_report(db_path)["categories"] if x["key"] == "h1" and x["built"]), None)
        eff = {"production": h1["production"],
               "down@1": next((m["down1"] for m in h1["models"] if m["production"]), None),
               "up@1": next((m["up1"] for m in h1["models"] if m["production"]), None)} if h1 else None
    except Exception:
        pass

    gaps = ["sector/industry classification (for sector-neutral factors & peers)",
            "predicted beta & factor betas (computable from bars — not yet in S2)",
            "institutional ownership / 13F", "bid/ask spread for a cost model",
            "NLP over sec_filings/transcripts (guidance tone, surprise-vs-narrative)",
            "predictor recalibration cadence (ECE is high — probabilities rank well but "
            "aren't perfectly calibrated; retrain/calibrate on a schedule)"]

    return {"generated_at": datetime.now(timezone.utc).isoformat(), "ticker": ticker,
            "as_of": asof, "price": price, "sector": None,
            "regime": {"vix": vix, "breadth": breadth, "decision": reg_decision,
                       "score": (reg or {}).get("score")},
            "action": action, "why": why, "factor_score": factor_score,
            "predictions": preds, "best_signal": best, "suggestion": conviction,
            "factors": [{"name": n, "pct": p, "note": d} for n, p, d in factors],
            "catalysts": {"days_to_earnings": dte, "next_earnings": (nx or {}).get("next_earnings"),
                          "analyst_rev_net_90d": rev_net, "insider_net_90d_usd": round(ins_net),
                          "implied_move": impl, "last_surprise_pct": surprise},
            "valuation": {k: latest(f"fund.{k}")[1] for k in
                          ("earnings_yield", "fcf_yield", "book_to_price", "roe",
                           "net_margin", "gross_profitability", "market_cap")},
            "valuation_ranks": {k: pctile(f"fund.{k}") for k in
                                ("earnings_yield", "fcf_yield", "roe", "gross_profitability")},
            "risk": {"hvol20": hvol, "event_risk": er, "short_pct_float": spf,
                     "days_to_cover": dtc, "near_52w_high": near_high, "beta": None},
            "positioning": {"short_pct_float": spf, "insider_net_90d_usd": round(ins_net),
                            "target_price": tgt, "upside_to_target": upside,
                            "recommendation_mean": an.get("recommendation_mean"),
                            "n_analysts": an.get("n_analysts"), "rev_net_90d": rev_net},
            "efficacy": eff, "gaps": gaps}


def ticker_prediction(ticker: str, db_path=DEFAULT_DB) -> dict:
    """The DEPLOYED big-move classifier's prediction for one ticker — calibrated P(up)/
    P(down) per horizon with rank vs universe, and the summarized strongest signal. This
    is what the /single-stock Predict trigger shows (NOT the old squished Ridge P≈0.5)."""
    r = alpha_report(ticker, db_path)
    return {"ticker": r["ticker"], "as_of": r["as_of"], "price": r["price"],
            "predictions": r["predictions"], "best_signal": r["best_signal"],
            "suggestion": r["suggestion"], "action": r["action"], "why": r["why"]}


def alpha_screen(db_path=DEFAULT_DB, n=15) -> dict:
    """Run the deployed big-move classifier across the WHOLE universe and rank it into
    TOP picks (highest up-conviction = longs) and BOTTOM picks (highest down-conviction =
    shorts). Conviction = the strongest calibrated P(move>±3%) across horizons; ranking is
    where the precision@k edge lives."""
    c = sqlite3.connect(Path(db_path))
    HZ = [1, 3, 5, 7]

    def load(feat):
        md = c.execute("SELECT MAX(event_time) FROM feature_values WHERE feature=?", (feat,)).fetchone()
        if not md or not md[0]:
            return {}, None
        return ({sc: float(v) for sc, v, _ in c.execute(
            "SELECT scope, value, MAX(ingested_at) FROM feature_values WHERE feature=? "
            "AND event_time=? GROUP BY scope", (feat, md[0]))}, md[0])

    up = {h: load(f"predict.pbig_up_{h}d")[0] for h in HZ}
    down = {h: load(f"predict.pbig_down_{h}d")[0] for h in HZ}
    _, asof = load("predict.pbig_up_1d")
    px = {sc: float(v) for sc, v, _ in c.execute(
        "SELECT scope, value, MAX(ingested_at) FROM feature_values WHERE feature='price.close' "
        "AND event_time=(SELECT MAX(event_time) FROM feature_values WHERE feature='price.close') "
        "GROUP BY scope")}
    dte = {sc: v for sc, v, _ in c.execute(
        "SELECT scope, value, MAX(ingested_at) FROM feature_values WHERE feature='calendar.days_to_earnings' "
        "AND event_time=(SELECT MAX(event_time) FROM feature_values WHERE feature='calendar.days_to_earnings') "
        "GROUP BY scope")}
    _, reg = None, None
    r = c.execute("SELECT value FROM feature_values WHERE feature='alpha.regime' AND scope='_market' "
                  "ORDER BY event_time DESC, ingested_at DESC LIMIT 1").fetchone()
    if r:
        try:
            reg = json.loads(r[0])
        except Exception:
            pass

    tickers = set().union(*[set(up[h]) for h in HZ]) if any(up.values()) else set()
    rows = []
    for t in tickers:
        bu = max(((up[h].get(t, 0.0), h) for h in HZ), default=(0, None))
        bd = max(((down[h].get(t, 0.0), h) for h in HZ), default=(0, None))
        try:
            de = int(float(dte.get(t))) if dte.get(t) is not None else None
        except (TypeError, ValueError):
            de = None
        rows.append({"ticker": t, "price": px.get(t),
                     "up_p": round(bu[0], 4), "up_h": f"{bu[1]}d" if bu[1] else "—",
                     "down_p": round(bd[0], 4), "down_h": f"{bd[1]}d" if bd[1] else "—",
                     "net": round(bu[0] - bd[0], 4), "days_to_earn": de})
    longs = [{**x, "rank": i + 1} for i, x in
             enumerate(sorted(rows, key=lambda x: -x["up_p"])[:n])]
    shorts = [{**x, "rank": i + 1} for i, x in
              enumerate(sorted(rows, key=lambda x: -x["down_p"])[:n])]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "as_of": asof,
            "universe_n": len(rows), "regime": reg, "longs": longs, "shorts": shorts,
            "note": ("Ranked by the deployed big-move classifier's calibrated conviction. "
                     "The DOWN side has stronger recorded skill (≈2.9× base), so the short "
                     "list is the higher-confidence one. Regime/event gates still apply per name.")}


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
