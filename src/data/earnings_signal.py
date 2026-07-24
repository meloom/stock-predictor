"""Grounded LLM extraction of earnings-report signals → fundamental.* features.

Ported from the predecessor (validated against known outcomes: correctly
called TSLA bearish / LMT bullish on their real 2026-07 Q2 reactions), with
two changes for the rebuild:
  1. Every call records its cost into the trigger's ledger (tokens + web
     searches) — S8's cost-per-trigger metric counts ALL calls a trigger
     initiates.
  2. Output is written to the feature store as `fundamental.earnings_signal`
     with event_time = report date (point-in-time rule: only valid after
     publication), not returned as a bespoke side-channel dict.

Hard rules (DESIGN.md S1): grounded search only — never classify without
evidence; fails closed (`insufficient_data`) on any error, never guesses.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from common.trigger import Trigger

ET = ZoneInfo("America/New_York")
SIGNAL_MODEL = "claude-sonnet-4-5"
RECENT_REPORT_WINDOW_DAYS = 5

SYSTEM_PROMPT = """You are an equity-research analyst extracting structured signals \
from a company's MOST RECENT quarterly earnings report, for a systematic trading \
system. Ground every factual claim in an actual web search this call — never assert \
a number or direction you have not just found. If you cannot find evidence of an \
earnings report within the lookback window, say so explicitly rather than guessing.

Extract these fields, in priority order (from a 65-report study of what actually \
explains next-day price reaction):

1. guidance_direction — the single strongest signal: raised, maintained, lowered, \
or withdrawn, with specific before/after numbers when found.
2. capex_trend and capex_framing — second strongest; can override guidance. Is capex \
accelerating/stable/decelerating, and is management framing it as monetizable growth \
(backlog, committed demand) or does it read as margin/FCF erosion? The same capex \
increase has produced opposite reactions depending on framing alone.
3. adj_eps_surprise_pct and revenue_surprise_pct — ADJUSTED (non-GAAP) vs consensus, \
NOT GAAP. Note any one-time items and the size of the GAAP/adjusted gap.
4. one_time_items — what drives any GAAP/adjusted divergence.

Respond with ONLY a JSON object (no markdown):
{
  "has_recent_report": true|false,
  "report_date": "YYYY-MM-DD" or null,
  "days_since_report": <int> or null,
  "guidance_direction": "raised"|"maintained"|"lowered"|"withdrawn"|"unknown",
  "guidance_detail": "<short>",
  "capex_trend": "accelerating"|"stable"|"decelerating"|"unknown",
  "capex_framing": "growth_positive"|"margin_concern"|"not_material"|"unknown",
  "adj_eps_surprise_pct": <float or null>,
  "revenue_surprise_pct": <float or null>,
  "one_time_items": [<short strings>],
  "net_signal": "bullish"|"bearish"|"neutral"|"insufficient_data",
  "confidence": "HIGH"|"MEDIUM"|"LOW",
  "reasoning": "<2-3 sentences>"
}
"""


def extract_earnings_signal(ticker: str, trigger: Trigger,
                            asof_date=None, client=None) -> dict:
    """One grounded extraction call. Cost lands on `trigger`. Fails closed."""
    import anthropic
    asof_date = asof_date or datetime.now(ET).date()
    asof_str = asof_date.isoformat() if hasattr(asof_date, "isoformat") else str(asof_date)
    client = client or anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    user_prompt = (
        f"Ticker: {ticker}\nAs-of date: {asof_str}\n\n"
        f"Search for whether {ticker} reported quarterly earnings within the last "
        f"{RECENT_REPORT_WINDOW_DAYS} trading days of {asof_str}. If yes, extract the "
        f"signals above from that report. If no recent report, set "
        f"has_recent_report=false — do not describe an older report as recent.")

    try:
        msg = client.messages.create(
            model=SIGNAL_MODEL,
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            messages=[{"role": "user", "content": user_prompt}],
        )
        usage = getattr(msg, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        server_tools = getattr(usage, "server_tool_use", None)
        searches = getattr(server_tools, "web_search_requests", 0) or 0
        trigger.record_cost(provider="claude-sonnet", tokens_in=tokens_in,
                            tokens_out=tokens_out, web_searches=searches,
                            note=f"earnings_signal:{ticker}")

        text = "".join(b.text for b in msg.content if b.type == "text")
        start, end = text.find("{"), text.rfind("}")
        result = json.loads(text[start:end + 1])
        result.setdefault("one_time_items", [])
        result["ticker"] = ticker
        return result
    except Exception as e:
        return {"ticker": ticker, "has_recent_report": False,
                "net_signal": "insufficient_data", "confidence": "LOW",
                "error": str(e)}
