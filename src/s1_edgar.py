"""s1_edgar.py — SEC EDGAR fetchers: the ACTUAL earnings report as document text
(8-K earnings releases, 10-Q/10-K with MD&A narrative) + XBRL full financials.

This is the authoritative, free, point-in-time-correct source. yfinance gives a thin
numeric summary; here we collect the real filing text (financials tables + management
narrative + guidance) so an S2 NLP layer can read it. event_time = the filing date (a
document is knowable once filed).

SEC fair-access rules: send a descriptive User-Agent with contact, and stay under
10 requests/second. We pace politely and the collector's rate limiter caps us too.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import date

SEC_UA = "stock-predictor research wl.gao@hotmail.com"   # SEC requires a contact UA
_BASE = "https://www.sec.gov"
_DATA = "https://data.sec.gov"
_PACE = 0.12                                             # ~8 req/s, under SEC's 10/s
COLLECTION_START = date(2025, 7, 1)

# focused but comprehensive set of standard us-gaap concepts = the financials table
XBRL_CONCEPTS = [
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "CostOfRevenue",
    "GrossProfit", "OperatingIncomeLoss", "ResearchAndDevelopmentExpense",
    "SellingGeneralAndAdministrativeExpense", "NetIncomeLoss",
    "EarningsPerShareBasic", "EarningsPerShareDiluted",
    "Assets", "AssetsCurrent", "Liabilities", "LiabilitiesCurrent", "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue", "InventoryNet",
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInInvestingActivities",
    "NetCashProvidedByUsedInFinancingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
]
_CALLS = 0                                               # HTTP calls in the current fetch


def _get(url: str) -> bytes:
    global _CALLS
    _CALLS += 1
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA,
                                               "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            data = gzip.decompress(data)
    time.sleep(_PACE)
    return data


def html_to_text(html: str) -> str:
    """Strip an EDGAR HTML document to readable plain text."""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)</(p|div|tr|br|h\d|li)>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&#\d+;|&\w+;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


_CIK_CACHE: dict[str, str] = {}


def ticker_cik_map() -> dict[str, str]:
    """ticker -> zero-padded 10-digit CIK (cached for the process)."""
    if not _CIK_CACHE:
        d = json.loads(_get(f"{_BASE}/files/company_tickers.json"))
        for v in d.values():
            _CIK_CACHE[v["ticker"].upper()] = str(v["cik_str"]).zfill(10)
    return _CIK_CACHE


def _exhibit991_url(cik: str, accn: str) -> str | None:
    """Find the earnings press release (Exhibit 99.1) inside an 8-K filing."""
    idx = json.loads(_get(f"{_DATA.replace('data', 'www')}/Archives/edgar/data/"
                          f"{int(cik)}/{accn}/index.json"))
    for item in idx.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if re.search(r"ex.?99", name, re.I) and name.endswith((".htm", ".html")):
            return f"{_BASE}/Archives/edgar/data/{int(cik)}/{accn}/{name}"
    return None


def collect_filings(ticker: str, since: str | None = None) -> tuple[list[dict], int]:
    """Every 8-K earnings release (Item 2.02) + 10-Q/10-K since `since`, as TEXT.
    Returns (rows, http_calls). Each row: form, filing_date, period, accession, url,
    raw_text — the real financials + narrative."""
    global _CALLS
    _CALLS = 0
    since = since or COLLECTION_START.isoformat()
    cik = ticker_cik_map().get(ticker.upper())
    if not cik:
        return [], _CALLS
    sub = json.loads(_get(f"{_DATA}/submissions/CIK{cik}.json"))
    rec = sub["filings"]["recent"]
    rows = []
    for form, fdate, accn, pdoc, items, rpt in zip(
            rec["form"], rec["filingDate"], rec["accessionNumber"],
            rec["primaryDocument"], rec.get("items", [""] * len(rec["form"])),
            rec.get("reportDate", [""] * len(rec["form"]))):
        if fdate < since:
            continue
        if form in ("10-Q", "10-K"):
            url = (f"{_BASE}/Archives/edgar/data/{int(cik)}/"
                   f"{accn.replace('-', '')}/{pdoc}")
        elif form == "8-K" and "2.02" in (items or ""):   # 2.02 = Results of Operations
            url = _exhibit991_url(cik, accn.replace("-", "")) or (
                f"{_BASE}/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{pdoc}")
        else:
            continue
        try:
            text = html_to_text(_get(url).decode("utf-8", "ignore"))
        except Exception:
            continue
        rows.append({"ticker": ticker.upper(), "cik": cik, "form": form,
                     "filing_date": fdate, "period_of_report": rpt or None,
                     "accession": accn, "url": url, "raw_text": text})
    return rows, _CALLS


def collect_xbrl(ticker: str, since: str | None = None) -> tuple[list[dict], int]:
    """Full financial-statement line items from EDGAR XBRL company-facts — every
    standard concept, every period since `since`. Returns (rows, http_calls)."""
    global _CALLS
    _CALLS = 0
    since = since or COLLECTION_START.isoformat()
    cik = ticker_cik_map().get(ticker.upper())
    if not cik:
        return [], _CALLS
    try:
        facts = json.loads(_get(f"{_DATA}/api/xbrl/companyfacts/CIK{cik}.json"))
    except Exception:
        return [], _CALLS
    gaap = facts.get("facts", {}).get("us-gaap", {})
    rows = []
    for concept in XBRL_CONCEPTS:
        node = gaap.get(concept)
        if not node:
            continue
        for unit, points in node.get("units", {}).items():
            for p in points:
                end = p.get("end")
                if not end or end < since:
                    continue
                rows.append({"ticker": ticker.upper(), "concept": concept,
                             "period_end": end, "fy": p.get("fy"), "fp": p.get("fp"),
                             "form": p.get("form"), "unit": unit,
                             "value": p.get("val"), "filed": p.get("filed")})
    return rows, _CALLS
