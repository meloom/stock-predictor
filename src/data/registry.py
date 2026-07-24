"""S1 feature declarations. Every feature S1 writes is declared here —
the store rejects unregistered writes, so this file IS the ingestion contract.
"""
from feature_store.store import FeatureStore

S1_FEATURES = [
    # name, dtype, scope_kind, cadence, point-in-time rule
    ("price.close",  "float", "ticker", "daily",
     "Known after the trading session close it describes."),
    ("price.volume", "float", "ticker", "daily",
     "Known after the trading session close it describes."),
    ("macro.vix",    "float", "market", "daily",
     "Known after the session close it describes."),
    ("macro.yield10y", "float", "market", "daily",
     "Known after the session close it describes."),
    ("macro.spy_close", "float", "market", "daily",
     "Known after the session close it describes."),
    ("calendar.days_to_earnings", "int", "ticker", "daily",
     "Computed from the published earnings calendar as of the ingestion "
     "moment; forward-looking by nature, revisable by the issuer."),
    ("fundamental.earnings_signal", "json", "ticker", "event",
     "Extracted from the most recent PUBLISHED earnings report via grounded "
     "search; event_time = report date; only valid after publication."),
]


def register_all(store: FeatureStore) -> None:
    for name, dtype, scope_kind, cadence, pit_rule in S1_FEATURES:
        store.register(name, dtype, scope_kind, source_stage="S1",
                       cadence=cadence, pit_rule=pit_rule)
