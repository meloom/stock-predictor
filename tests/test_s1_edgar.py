"""Offline tests for the SEC EDGAR fetchers (no network — the parsing/text logic)."""
import s1_edgar


def test_html_to_text_strips_and_normalizes():
    html = ("<html><head><style>.x{color:red}</style></head><body>"
            "<p>Apple posted <b>$111.2 billion</b>, up 17%</p>"
            "<div>Tim Cook said demand was strong.</div>"
            "<script>ignore()</script></body></html>")
    text = s1_edgar.html_to_text(html)
    assert "$111.2 billion" in text and "up 17%" in text
    assert "Tim Cook said demand was strong." in text
    assert "ignore()" not in text and "color:red" not in text   # script/style dropped
    assert "<" not in text and ">" not in text                  # no tags remain


def test_xbrl_concepts_cover_the_core_statements():
    c = set(s1_edgar.XBRL_CONCEPTS)
    # income statement, balance sheet, cash flow all represented
    assert {"NetIncomeLoss", "GrossProfit", "EarningsPerShareDiluted"} <= c
    assert {"Assets", "Liabilities", "StockholdersEquity"} <= c
    assert "NetCashProvidedByUsedInOperatingActivities" in c


def test_sec_signals_registered_as_event_frequency():
    from collector import default_collector, KIND_TABLE, _sig_enabled
    col = default_collector()
    for kind, table in (("sec_filings", "sec_filings"), ("xbrl", "xbrl_financials")):
        assert col.kinds[kind]["frequency"] == "event"
        assert KIND_TABLE[kind] == table
    assert col.kinds["sec_filings"]["source"] == "sec"
    # transcript is a PAID/blocked source, disabled in config -> NOT registered (so it
    # can't spam failures). KIND_TABLE still maps it for when it's re-enabled.
    assert KIND_TABLE["transcript"] == "transcripts"
    assert _sig_enabled("transcript") is False
    assert "transcript" not in col.kinds
