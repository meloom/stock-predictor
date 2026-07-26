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
from core import DEFAULT_DB                                          # noqa: E402

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
                 "calendar.days_to_earnings", "predict.eod_return"],
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
