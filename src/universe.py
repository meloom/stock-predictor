"""universe.py — the canonical list of tickers the system tracks.

Single source of truth for "all the tickers we track", shared by the pipeline
and by modeling so training always covers the same names the system trades.
Liquid US large/mid-caps across sectors. Edit here to change coverage.
"""

UNIVERSE = [
    # mega-cap tech / semis
    "AAPL", "MSFT", "NVDA", "AMD", "INTC", "MRVL", "AVGO", "QCOM", "MU", "TXN",
    "AMAT", "LRCX", "KLAC", "ADI", "NXPI", "ON", "MCHP",
    # internet / software
    "AMZN", "GOOGL", "META", "NFLX", "CRM", "ORCL", "ADBE", "NOW", "SHOP",
    "SNOW", "PLTR", "DDOG", "NET", "PANW", "FTNT", "CRWD", "ZS", "TEAM", "MDB",
    "OKTA", "TTD", "RBLX", "COIN", "HOOD", "APP", "UBER", "ABNB", "DASH",
    # financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "AXP", "SCHW", "BLK",
    "SPGI", "CME", "PYPL",
    # healthcare
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR", "AMGN",
    "GILD", "ISRG", "MRNA", "VRTX", "REGN", "BSX",
    # consumer
    "HD", "COST", "WMT", "TGT", "LOW", "NKE", "SBUX", "MCD", "PG", "KO", "PEP",
    "PM", "CL", "DIS", "CMCSA",
    # industrials / energy / materials
    "CAT", "DE", "BA", "GE", "HON", "UPS", "UNP", "LMT", "RTX", "XOM", "CVX",
    "COP", "SLB", "FCX", "NEM", "LIN",
    # autos / misc
    "TSLA", "F", "GM",
]

assert len(UNIVERSE) == len(set(UNIVERSE)), "duplicate ticker in UNIVERSE"
