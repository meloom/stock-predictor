"""modeling/augment_features.py — feature blocks discovered from ERROR ANALYSIS.

Each block is a family of point-in-time-safe features motivated by a concrete
cluster of confident-but-wrong predictions (see modeling/ERROR_ANALYSIS.md). The
rule we hold to: the feature must be knowable at the close of day `d` — we never
feed the *reason* a stock moved (that is the label leaking back in), only signals
that were observable BEFORE the move.

Blocks (added one per improvement-loop iteration):
  - "ext"    overextension / mean-reversion. Motivated by MRVL/LRCX/AMAT/KLAC all
             called UP with 0.9+ confidence on 2026-06-30 and each crashing ~10%:
             external analysis says they "peaked on June 30" — the model was most
             confident-up exactly at the local top. These features tell it when a
             name is stretched (at range highs, many up-days, high z-score).
  - "sector" sector-relative strength / crowding. Same 2026-06-30 event: the four
             wrong calls were ALL semiconductors moving together. Per-day sector
             mean momentum + this name's momentum minus its sector captures the
             co-movement a per-name model is blind to.

Bars are fetched ONCE and cached to runtime/bars_1y.pkl so repeated loop
iterations never re-hit yfinance.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "modeling"))
import harness as H                                    # noqa: E402
from s1_data import fetch_daily_bars, fetch_macro      # noqa: E402
from universe import UNIVERSE                          # noqa: E402

BARS_CACHE = ROOT / "runtime" / "bars_1y.pkl"
MACRO_CACHE = ROOT / "runtime" / "macro_1y.pkl"
INSIDER_CACHE = ROOT / "runtime" / "insider.pkl"
EARNINGS_CACHE = ROOT / "runtime" / "earnings.pkl"
ANALYST_CACHE = ROOT / "runtime" / "analyst_revisions.pkl"

# coarse sector map for the tracked universe (only groups we actually hold need
# entries; anything unlisted falls in "other" and gets a neutral sector signal).
SECTORS = {
    "semis": ["NVDA", "AMD", "MRVL", "LRCX", "AMAT", "KLAC", "ON", "MU", "INTC",
              "TXN", "QCOM", "AVGO", "ASML", "TSM", "ADI", "MCHP", "NXPI", "SWKS"],
    "software": ["MSFT", "ORCL", "CRM", "ADBE", "NOW", "SNOW", "MDB", "NET", "DDOG",
                 "SHOP", "APP", "PLTR", "TEAM", "PANW", "CRWD", "ZS", "WDAY", "INTU"],
    "megacap_tech": ["AAPL", "GOOGL", "GOOG", "META", "AMZN", "TSLA", "NFLX"],
    "financials": ["JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "AXP", "SCHW"],
    "health": ["UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR"],
    "consumer": ["WMT", "COST", "HD", "NKE", "MCD", "SBUX", "TGT", "LOW", "PG", "KO", "PEP"],
    "industrial": ["CAT", "BA", "GE", "HON", "UPS", "GM", "F", "DE", "LMT", "RTX"],
    "energy": ["XOM", "CVX", "COP", "SLB", "EOG", "OXY"],
}
_TICKER_SECTOR = {t: s for s, ts in SECTORS.items() for t in ts}


def get_bars(period: str = "1y") -> dict:
    if BARS_CACHE.exists():
        return pickle.load(open(BARS_CACHE, "rb"))
    bars = fetch_daily_bars(UNIVERSE, period=period)
    BARS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    pickle.dump(bars, open(BARS_CACHE, "wb"))
    return bars


def get_macro(period: str = "1y") -> dict:
    if MACRO_CACHE.exists():
        return pickle.load(open(MACRO_CACHE, "rb"))
    m = fetch_macro(period=period)
    MACRO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    pickle.dump(m, open(MACRO_CACHE, "wb"))
    return m


def get_insider(tickers=None) -> dict:
    """Fetch insider transactions once (per-ticker, resilient) and cache. Value =
    list of {date, value, position, is_sale} per ticker. Missing/failed ticker ->
    empty list (feature becomes NaN downstream — honest, never faked)."""
    if INSIDER_CACHE.exists():
        return pickle.load(open(INSIDER_CACHE, "rb"))
    import yfinance as yf
    tickers = tickers or UNIVERSE
    out = {}
    for t in tickers:
        rows = []
        try:
            df = yf.Ticker(t).insider_transactions
            if df is not None and len(df):
                for _, r in df.iterrows():
                    d = r.get("Start Date")
                    d = d.date().isoformat() if hasattr(d, "date") else (str(d)[:10] if d is not None else None)
                    txt = str(r.get("Text", "")); pos = str(r.get("Position", ""))
                    val = r.get("Value")
                    val = float(val) if val is not None and str(val) != "nan" else 0.0
                    is_sale = ("sale" in txt.lower()) and val > 0     # open-market sale only
                    if d:
                        rows.append({"date": d, "value": val, "position": pos, "is_sale": is_sale})
        except Exception:
            pass
        out[t] = rows
    INSIDER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    pickle.dump(out, open(INSIDER_CACHE, "wb"))
    return out


def get_earnings(tickers=None) -> dict:
    """Fetch each ticker's earnings-announcement dates once (past + scheduled
    future) and cache. yfinance get_earnings_dates gives ~6y of quarterly dates.
    Missing/failed -> empty list (feature NaN downstream)."""
    if EARNINGS_CACHE.exists():
        return pickle.load(open(EARNINGS_CACHE, "rb"))
    import yfinance as yf
    tickers = tickers or UNIVERSE
    out = {}
    for t in tickers:
        dates = []
        try:
            df = yf.Ticker(t).get_earnings_dates(limit=24)
            if df is not None and len(df):
                dates = sorted({str(i)[:10] for i in df.index})
        except Exception:
            pass
        out[t] = dates
    EARNINGS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    pickle.dump(out, open(EARNINGS_CACHE, "wb"))
    return out


def get_analyst_revisions(tickers=None) -> dict:
    """Fetch each ticker's dated analyst grade changes (upgrades/downgrades) once
    and cache. Value = list of {date, action} where action in up/down/main/init.
    This is the top ex-ante DIRECTIONAL earnings signal (EARNINGS_RESEARCH.md):
    net revision momentum predicts the surprise direction better than the surprise.
    Dated -> PIT-safe. Missing/failed ticker -> [] (feature NaN)."""
    if ANALYST_CACHE.exists():
        return pickle.load(open(ANALYST_CACHE, "rb"))
    import yfinance as yf
    tickers = tickers or UNIVERSE
    out = {}
    for t in tickers:
        rows = []
        try:
            df = yf.Ticker(t).upgrades_downgrades
            if df is not None and len(df):
                for idx, r in df.iterrows():
                    d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
                    act = str(r.get("Action", "")).lower()
                    rows.append({"date": d, "action": act})
        except Exception:
            pass
        out[t] = rows
    ANALYST_CACHE.parent.mkdir(parents=True, exist_ok=True)
    pickle.dump(out, open(ANALYST_CACHE, "wb"))
    return out


def analyst_block(meta, bars):
    """ANALYST-REVISION momentum — net upgrades-minus-downgrades trailing 30d/90d
    plus a recent-downgrade flag. PIT-safe: only grade changes dated <= d. The
    strongest ex-ante directional earnings signal in the research."""
    from datetime import date
    rev = get_analyst_revisions()
    ords = {}
    for t, rows in rev.items():
        acc = []
        for r in rows:
            try:
                acc.append((date(*map(int, r["date"].split("-"))).toordinal(), r["action"]))
            except Exception:
                pass
        ords[t] = sorted(acc)
    names = ["an.net_rev_30d", "an.net_rev_90d", "an.recent_downgrade_30d"]
    F = np.full((len(meta), len(names)), np.nan)
    for r, (d, t) in enumerate(meta):
        acc = ords.get(t)
        if acc is None:
            continue
        do = date(*map(int, d.split("-"))).toordinal()
        n30u = n30d = n90u = n90d = 0
        for eo, act in acc:
            if eo > do:
                break
            age = do - eo
            up = act == "up"; dn = act == "down"
            if age <= 90:
                n90u += up; n90d += dn
            if age <= 30:
                n30u += up; n30d += dn
        F[r, 0] = n30u - n30d
        F[r, 1] = n90u - n90d
        F[r, 2] = 1.0 if n30d > 0 else 0.0
    return names, F


def preearn_block(meta, bars, win: int = 10):
    """PRE-EARNINGS DRIFT — the price move INTO the report (professionals position
    ahead), gated to names reporting within `win` sessions. Trailing 5-day and
    10-day return, zero when no earnings are near (so the feature only speaks when
    a catalyst is imminent). PIT-safe (trailing closes + scheduled calendar)."""
    from datetime import date
    S = _series(bars)
    cal = get_earnings()
    ords = {t: sorted(date(*map(int, d.split("-"))).toordinal() for d in ds)
            for t, ds in cal.items()}
    names = ["pe.drift5_near", "pe.drift10_near", "pe.near_earn_flag"]
    F = np.full((len(meta), len(names)), np.nan)
    for r, (d, t) in enumerate(meta):
        s = S.get(t); es = ords.get(t)
        if not s or d not in s["idx"]:
            continue
        do = date(*map(int, d.split("-"))).toordinal()
        nxt = [e - do for e in es] if es else []
        nxt = [x for x in nxt if x >= 0]
        near = 1.0 if (nxt and min(nxt) <= win) else 0.0
        i = s["idx"][d]; c = s["c"]
        d5 = (c[i] / c[i - 5] - 1.0) if i >= 5 else 0.0
        d10 = (c[i] / c[i - 10] - 1.0) if i >= 10 else 0.0
        F[r, 0] = d5 * near
        F[r, 1] = d10 * near
        F[r, 2] = near
    return names, F


def earnings_block(meta, bars, cap: int = 90):
    """EARNINGS-PROXIMITY — the dominant RECALL gap (modeling/ERROR_ANALYSIS.md):
    every top-10 missed move was a 20-31% one-day earnings reaction the model gave
    ~0 probability, because it had no earnings channel. Features (all knowable in
    advance from the scheduled calendar, PIT-safe): calendar days to the next
    earnings, days since the last, and flags for 'reaction imminent' (report is
    today/tomorrow -> next session is the gap) and 'just reported'. Direction of
    the surprise is NOT predictable — the value is letting the model ABSTAIN into a
    known catalyst (kills precision failures) and flag big-move risk (recall)."""
    from datetime import date
    cal = get_earnings()
    ords = {t: sorted(date(*map(int, d.split("-"))).toordinal() for d in ds)
            for t, ds in cal.items()}
    names = ["eve.days_to_earn", "eve.days_since_earn", "eve.earn_imminent", "eve.post_earn"]
    F = np.full((len(meta), len(names)), np.nan)
    for r, (d, t) in enumerate(meta):
        es = ords.get(t)
        if not es:
            continue
        d_ord = date(*map(int, d.split("-"))).toordinal()
        nxt = [e - d_ord for e in es if e >= d_ord]
        prev = [d_ord - e for e in es if e <= d_ord]
        dto = min(nxt) if nxt else cap
        dsi = min(prev) if prev else cap
        F[r, 0] = min(dto, cap)
        F[r, 1] = min(dsi, cap)
        F[r, 2] = 1.0 if dto <= 1 else 0.0     # report today/tomorrow -> reaction next session
        F[r, 3] = 1.0 if dsi <= 1 else 0.0     # just reported (post-earnings drift)
    return names, F


def insider_block(meta, bars, win: int = 90):
    """INSIDER-SELLING — recurring driver: AMAT CEO sold $55.5M on 2026-06-30 (the
    crash day) + CTO mid-June; MRVL CEO sold at the top. Cross-sectionally
    comparable, magnitude-free (we have no reliable market cap): trailing counts of
    open-market SALES and a C-suite-sold flag. PIT-safe: only transactions with
    Start Date <= d. A stretch of insiders selling into strength is a reversal
    warning the price/volume features can't see."""
    ins = get_insider()
    # pre-sort each ticker's sale events by date
    S = {t: sorted([r for r in rows if r["is_sale"]], key=lambda r: r["date"])
         for t, rows in ins.items()}
    names = ["ins.sale_cnt_90d", "ins.sale_cnt_30d", "ins.csuite_sale_60d"]
    from datetime import date

    def dsub(dstr, days):
        y, m, dd = map(int, dstr.split("-"))
        return (date(y, m, dd).toordinal() - days)
    F = np.full((len(meta), len(names)), np.nan)
    for r, (d, t) in enumerate(meta):
        rows = S.get(t)
        if rows is None:
            continue
        d_ord = date(*map(int, d.split("-"))).toordinal()
        c90 = c30 = 0; csuite = 0.0
        for ev in rows:
            e_ord = date(*map(int, ev["date"].split("-"))).toordinal()
            if e_ord > d_ord:            # future transaction — not knowable yet (PIT)
                continue
            age = d_ord - e_ord
            if age <= 90:
                c90 += 1
            if age <= 30:
                c30 += 1
            if age <= 60 and any(k in ev["position"].lower() for k in ("chief", "president")):
                csuite = 1.0
        F[r, 0] = min(c90, 20); F[r, 1] = min(c30, 20); F[r, 2] = csuite
    return names, F


def _series(bars: dict) -> dict:
    """ticker -> {'dates':[...], 'idx':{date:i}, 'c':arr, 'h':arr, 'l':arr, 'o':arr}."""
    out = {}
    for t, rows in bars.items():
        rows = sorted(rows, key=lambda r: r["date"])
        if not rows:
            continue
        dates = [r["date"] for r in rows]
        out[t] = {
            "dates": dates, "idx": {d: i for i, d in enumerate(dates)},
            "c": np.array([r["close"] for r in rows], float),
            "h": np.array([r.get("high", r["close"]) for r in rows], float),
            "l": np.array([r.get("low", r["close"]) for r in rows], float),
            "o": np.array([r.get("open", r["close"]) for r in rows], float),
        }
    return out


def ext_block(meta, bars, win: int = 20):
    """Overextension / mean-reversion features, PIT-safe (known at close of d)."""
    S = _series(bars)
    names = ["ext.dist_hi20", "ext.dist_lo20", "ext.close_z20", "ext.range_pos20",
             "ext.consec_up", "ext.ret_cum5", "ext.gap_today"]
    F = np.full((len(meta), len(names)), np.nan)
    for r, (d, t) in enumerate(meta):
        s = S.get(t)
        if not s or d not in s["idx"]:
            continue
        i = s["idx"][d]
        if i < win:
            continue
        c, h, l, o = s["c"], s["h"], s["l"], s["o"]
        c0 = c[i]
        hw, lw, cw = h[i - win + 1:i + 1], l[i - win + 1:i + 1], c[i - win + 1:i + 1]
        hi, lo = hw.max(), lw.min()
        sd = cw.std() or 1.0
        # consecutive up-days ending at i
        cu = 0
        for k in range(i, 0, -1):
            if c[k] > c[k - 1]:
                cu += 1
            else:
                break
        F[r, 0] = (c0 - hi) / hi                       # <=0, near 0 = at 20d high
        F[r, 1] = (c0 - lo) / lo                       # >=0, near 0 = at 20d low
        F[r, 2] = (c0 - cw.mean()) / sd                # z-score vs 20d mean
        F[r, 3] = (c0 - lo) / (hi - lo) if hi > lo else 0.5  # 0..1 range position
        F[r, 4] = min(cu, 10)                           # capped run length
        F[r, 5] = c0 / c[i - 5] - 1.0                   # 5-day cumulative return
        F[r, 6] = o[i] / c[i - 1] - 1.0                 # today's opening gap
    return names, F


def sector_block(meta, bars, win: int = 20):
    """Sector-relative strength: this name's 20d momentum minus its sector's
    mean 20d momentum on the same day, plus the sector mean itself. Captures the
    crowding a per-name model can't see (the whole semis group topping together)."""
    S = _series(bars)
    # precompute each ticker's 20d momentum per date
    mom = {}
    for t, s in S.items():
        c = s["c"]; m = np.full(len(c), np.nan)
        m[win:] = c[win:] / c[:-win] - 1.0
        mom[t] = m
    # sector mean momentum per (date, sector)
    dates = sorted({d for d, _ in meta})
    sec_mean = {}
    for d in dates:
        acc = {}
        for t, s in S.items():
            if d not in s["idx"]:
                continue
            i = s["idx"][d]
            v = mom[t][i]
            if np.isnan(v):
                continue
            sec = _TICKER_SECTOR.get(t, "other")
            acc.setdefault(sec, []).append(v)
        sec_mean[d] = {k: float(np.mean(v)) for k, v in acc.items()}
    names = ["sec.mom_mean", "sec.rel_strength"]
    F = np.full((len(meta), len(names)), np.nan)
    for r, (d, t) in enumerate(meta):
        s = S.get(t)
        if not s or d not in s["idx"]:
            continue
        sec = _TICKER_SECTOR.get(t, "other")
        sm = sec_mean.get(d, {}).get(sec)
        mv = mom[t][s["idx"][d]]
        if sm is None or np.isnan(mv):
            continue
        F[r, 0] = sm
        F[r, 1] = mv - sm
    return names, F


def xhorizon_block(meta, bars):
    """LONG-horizon extension — the driver my 20-day `ext` block was blind to.
    Every top up-failure (MRVL +300% YTD, AMAT all-time high $739, MU ~$1200) was
    a name at/near a 52-week high after a multi-month parabolic run, then a
    'sell-the-news'/profit-take reversal. These measure that: multi-month
    returns, distance from the trailing 252-day high, and a new-high flag. All
    known at the close of day d, PIT-safe."""
    S = _series(bars)
    names = ["xh.ret_21d", "xh.ret_63d", "xh.ret_126d", "xh.dist_hi252",
             "xh.new_high_flag", "xh.above_hi_streak"]
    F = np.full((len(meta), len(names)), np.nan)
    for r, (d, t) in enumerate(meta):
        s = S.get(t)
        if not s or d not in s["idx"]:
            continue
        i = s["idx"][d]
        if i < 40:                                   # need a few months of history
            continue
        c = s["c"]; c0 = c[i]
        lb = min(252, i)
        hi = c[i - lb + 1:i + 1].max()
        F[r, 0] = c0 / c[i - 21] - 1.0 if i >= 21 else np.nan     # 1-month return
        F[r, 1] = c0 / c[i - 63] - 1.0 if i >= 63 else np.nan     # 3-month return
        F[r, 2] = c0 / c[i - 126] - 1.0 if i >= 126 else np.nan   # 6-month return
        F[r, 3] = (c0 - hi) / hi                                  # <=0, 0 = at 252d high
        F[r, 4] = 1.0 if c0 >= 0.98 * hi else 0.0                # within 2% of the high
        # how many of the last 10 days closed within 3% of the trailing high
        streak = 0
        for k in range(i, max(i - 10, lb - 1), -1):
            hk = c[max(0, k - lb + 1):k + 1].max()
            if c[k] >= 0.97 * hk:
                streak += 1
            else:
                break
        F[r, 5] = streak
    return names, F


def macro_block(meta, bars, win: int = 20):
    """MARKET/RATES regime — the S3 model had no macro input. The late-June-2026
    selloff was a rates event: a strong jobs report spiked the 10-year yield,
    de-rating high-multiple tech hardest. Features: 10Y yield level & 5-day
    change, VIX level & 5-day change (from S1's ^TNX/^VIX), plus the key
    interaction — 5-day yield change times this name's 1-month run-up, because
    rising yields punish the most-extended names most. Day-level, PIT-safe."""
    S = _series(bars)
    macro = get_macro("1y")

    def to_map(series):
        return {pt["date"]: float(pt["value"]) for pt in series if pt.get("value") is not None}
    yld = to_map(macro.get("yield10y", []))
    vix = to_map(macro.get("vix", []))
    yd = sorted(yld); vd = sorted(vix)
    yi = {d: i for i, d in enumerate(yd)}; vi = {d: i for i, d in enumerate(vd)}
    names = ["mac.yield_level", "mac.yield_chg5", "mac.vix_level", "mac.vix_chg5",
             "mac.yield_chg5_x_runup"]
    F = np.full((len(meta), len(names)), np.nan)
    for r, (d, t) in enumerate(meta):
        ylev = yld.get(d); vlev = vix.get(d)
        ychg = (ylev - yld[yd[yi[d] - 5]]) if d in yi and yi[d] >= 5 else np.nan
        vchg = (vlev - vix[vd[vi[d] - 5]]) if d in vi and vi[d] >= 5 else np.nan
        F[r, 0] = ylev if ylev is not None else np.nan
        F[r, 1] = ychg
        F[r, 2] = vlev if vlev is not None else np.nan
        F[r, 3] = vchg
        s = S.get(t)
        if s and d in s["idx"] and s["idx"][d] >= 21 and not np.isnan(ychg):
            i = s["idx"][d]
            runup = s["c"][i] / s["c"][i - 21] - 1.0
            F[r, 4] = ychg * runup                    # rising yields x recent run-up
    return names, F


def regime_block(meta, bars, win: int = 20):
    """Day-level MARKET-REGIME features, broadcast to every name on that day.
    The per-name S3 model is blind to market context; on 2026-06-30 the whole
    tape rolled over (memory-price / China-AI macro fear) and the model kept
    buying extended semis. These features (equal-weight-universe proxy for the
    market) tell it when breadth is collapsing / the tape is weak, so it can
    stop adding risk into a broad selloff. All known at the close of day d."""
    S = _series(bars)
    # equal-weight market close index across the universe, aligned by date
    dates = sorted({d for d, _ in meta})
    # per-ticker close-by-date and above-MA flag
    idx_of = {t: s["idx"] for t, s in S.items()}
    names = ["mkt.ret5", "mkt.vs_ma20", "mkt.breadth", "mkt.dispersion", "mkt.stress"]
    # precompute market series over the union of dates present in bars
    all_dates = sorted({d for s in S.values() for d in s["dates"]})
    mkt_ret1 = {}      # date -> equal-weight 1d return
    breadth = {}       # date -> fraction of names above their 20d MA
    disp = {}          # date -> cross-sectional std of 1d returns
    for di, d in enumerate(all_dates):
        rets, above = [], []
        for t, s in S.items():
            i = s["idx"].get(d)
            if i is None or i < win:
                continue
            c = s["c"]
            rets.append(c[i] / c[i - 1] - 1.0)
            above.append(1.0 if c[i] > c[i - win:i].mean() else 0.0)
        if rets:
            mkt_ret1[d] = float(np.mean(rets)); disp[d] = float(np.std(rets))
            breadth[d] = float(np.mean(above))
    md = sorted(mkt_ret1)
    mpos = {d: i for i, d in enumerate(md)}
    mvals = np.array([mkt_ret1[d] for d in md])
    mlevel = np.cumprod(1 + mvals)                    # market index level
    F = np.full((len(meta), len(names)), np.nan)
    for r, (d, t) in enumerate(meta):
        if d not in mpos:
            continue
        j = mpos[d]
        if j < win:
            continue
        F[r, 0] = mlevel[j] / mlevel[j - 5] - 1.0                     # market 5d return
        F[r, 1] = mlevel[j] / mlevel[j - win:j].mean() - 1.0          # market vs 20d MA
        F[r, 2] = breadth.get(d, np.nan)                             # % universe above 20d MA
        F[r, 3] = disp.get(d, np.nan)                               # cross-sectional dispersion
        recent = mvals[j - 5:j + 1]
        F[r, 4] = float(np.std(recent))                             # recent market volatility (stress)
    return names, F


BLOCKS = {"ext": ext_block, "sector": sector_block, "regime": regime_block,
          "xhorizon": xhorizon_block, "macro": macro_block, "insider": insider_block,
          "earnings": earnings_block, "analyst": analyst_block, "preearn": preearn_block}


def build_augmented(block_keys: list[str], horizon: int = 1):
    """Load the standardized full dataset and append the requested feature blocks.
    Returns {X, y, meta, feature_names, base_names, added_names}."""
    ds = H.load_full_dataset(horizon)
    if ds is None:
        raise SystemExit("no full dataset — build it first.")
    p = ds["panel"]
    X, y, meta = p["X"], np.asarray(p["y"], float), p["meta"]
    base_names = list(p["feature_names"])
    bars = get_bars()
    cols, added = [X], []
    for k in block_keys:
        names, F = BLOCKS[k](meta, bars)
        cols.append(F)
        added += names
    Xa = np.hstack(cols)
    return {"X": Xa, "y": y, "meta": meta,
            "feature_names": base_names + added,
            "base_names": base_names, "added_names": added}
