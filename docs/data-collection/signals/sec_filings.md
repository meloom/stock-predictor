# Signal: `sec_filings` / `xbrl` / `transcript` — the earnings report as a DOCUMENT

The `earnings_reports` (yfinance) signal is a thin numeric summary. The **actual earnings
report** is a filed document with the full financial tables *and* management narrative +
forward guidance. These signals collect that.

| | |
|---|---|
| **Collector kinds** | `sec_filings` (text), `xbrl` (full financials), `transcript` (paid) |
| **Source** | SEC EDGAR (free, authoritative, PIT-correct) · transcript = paid API |
| **Frequency** | `event` — collect all filings from the last watermark → now |
| **Typed tables** | `sec_filings`, `xbrl_financials`, `transcripts` |
| **Source timestamp** | `filing_date` / `period_end` / `call_date` |

## Why (the gap this closes)
yfinance gives ~6 numeric fields. The real report has: full financial-statement tables,
Management Discussion & Analysis (MD&A), and forward guidance narrative. Storing the
**document text** lets an S2 NLP layer read tone, guidance, and surprise-vs-narrative —
closing the model's "news-blind" gap. `event_time` = filing date (knowable once filed).

## `sec_filings` — 8-K earnings releases + 10-Q/10-K as TEXT
Per ticker: map ticker→CIK (`company_tickers.json`), list filings since `COLLECTION_START`,
keep **8-K Item 2.02** (earnings release, Exhibit 99.1) and **10-Q/10-K** (financials +
MD&A), download each, strip HTML → `raw_text`.

```sql
CREATE TABLE sec_filings (
  ticker TEXT, cik TEXT, form TEXT, period_of_report TEXT,
  accession TEXT, url TEXT, raw_text TEXT,     -- the full filing text
  filing_date TEXT,                            -- SOURCE timestamp
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, accession)
);
```
Verified live (AAPL): 10-K = 222,888 chars, 10-Q = 97,699 chars, 8-K earnings release =
11,128 chars — real text incl. *"Apple today announced financial results … revenue of
$111.2 billion, up 17 percent … Tim Cook said …"*.

## `xbrl` — full financial-statement line items
EDGAR XBRL company-facts (`/api/xbrl/companyfacts/CIK…json`, 1 call/ticker): every
standard us-gaap concept × period since `COLLECTION_START` — the complete numeric table.

```sql
CREATE TABLE xbrl_financials (
  ticker TEXT, concept TEXT, fy REAL, fp TEXT, form TEXT, unit TEXT,
  value REAL, filed TEXT,
  period_end TEXT,                             -- SOURCE timestamp
  ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, concept, period_end, form)
);
```
Example rows (AAPL): `RevenueFromContractWithCustomerExcludingAssessedTax` — 2026-03-28 =
$254.94B; `NetIncomeLoss`, `GrossProfit`, `Assets`, `StockholdersEquity`,
`NetCashProvidedByUsedInOperatingActivities`, EPS, etc.

## `transcript` — earnings-call transcript (PAID, currently BLOCKED)
The richest forward-looking narrative (guidance + analyst Q&A) is NOT on EDGAR. Needs a
paid provider (Seeking Alpha / FMP / API Ninjas, ~$20–100+/mo). The handler raises until
`TRANSCRIPT_API_KEY` + a provider are configured, so the dashboard shows it **blocked**
(honest coverage) rather than a false success.

```sql
CREATE TABLE transcripts (
  ticker TEXT, quarter TEXT, source TEXT, raw_text TEXT,
  call_date TEXT, ingested_at TEXT NOT NULL,
  PRIMARY KEY (ticker, call_date)
);
```

## After processing (S2 — NLP layer)
```text
sec_filings.raw_text / transcripts.raw_text →
  guidance extraction, management-tone sentiment, surprise-vs-narrative divergence,
  risk-factor deltas quarter-over-quarter  → S3 features
xbrl_financials → authoritative value factors (replaces the thin yfinance numbers)
```

## Coverage & rate limits
Event mode: coverage = tickers covered + records + span. SEC fair-access: descriptive
User-Agent + <10 req/s (source `sec` capped at 8/s; handler paces ~0.12s/call).
`sec_filings` ≈ 6–10 filings/ticker since Jul 2025; `xbrl` = 1 call → dozens of rows.
