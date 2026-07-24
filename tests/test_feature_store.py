"""Feature store contract tests — the guarantees DESIGN.md promises."""
import pytest

from feature_store.store import FeatureStore, UnregisteredFeatureError


@pytest.fixture
def store(tmp_path):
    return FeatureStore(tmp_path / "test.db")


def _register_close(store):
    store.register("price.close", "float", "ticker", "S1", "daily",
                   "known after session close")


def test_unregistered_write_rejected(store):
    with pytest.raises(UnregisteredFeatureError):
        store.write("nope.feature", "AAPL", "2026-07-24", 1.0, trigger_id="t1")


def test_register_idempotent_but_spec_change_raises(store):
    _register_close(store)
    _register_close(store)  # identical re-register: fine
    with pytest.raises(ValueError):
        store.register("price.close", "int", "ticker", "S1", "daily", "different")


def test_read_asof_returns_latest_event_time(store):
    _register_close(store)
    store.write("price.close", "AAPL", "2026-07-22", 100.0, trigger_id="t1")
    store.write("price.close", "AAPL", "2026-07-23", 101.0, trigger_id="t1")
    rec = store.read_asof("price.close", "AAPL", "2026-07-24")
    assert rec["value"] == 101.0
    assert rec["event_time"] == "2026-07-23"


def test_as_known_at_hides_later_ingestion(store):
    """The lookahead guarantee: a backtest asking 'what did we know at T'
    must not see values ingested after T — even for earlier event_times."""
    _register_close(store)
    store.write("price.close", "AAPL", "2026-07-22", 100.0,
                trigger_id="t1", ingested_at="2026-07-22T21:00:00+00:00")
    # correction ingested two days later
    store.write("price.close", "AAPL", "2026-07-22", 99.5,
                trigger_id="t2", ingested_at="2026-07-24T09:00:00+00:00")

    known_early = store.read_asof("price.close", "AAPL", "2026-07-23",
                                  as_known_at="2026-07-23T00:00:00+00:00")
    assert known_early["value"] == 100.0  # correction invisible at that time
    known_late = store.read_asof("price.close", "AAPL", "2026-07-23")
    assert known_late["value"] == 99.5    # latest version otherwise


def test_lineage_trigger_id_preserved(store):
    _register_close(store)
    store.write("price.close", "AAPL", "2026-07-24", 100.0, trigger_id="trig-abc")
    assert store.read_asof("price.close", "AAPL", "2026-07-24")["trigger_id"] == "trig-abc"


def test_freshness_reports_latest(store):
    _register_close(store)
    assert store.freshness("price.close") is None
    store.write("price.close", "AAPL", "2026-07-23", 1.0, trigger_id="t")
    store.write("price.close", "MSFT", "2026-07-24", 2.0, trigger_id="t")
    fr = store.freshness("price.close")
    assert fr["latest_event_time"] == "2026-07-24"


def test_read_panel_all_scopes(store):
    _register_close(store)
    store.write("price.close", "AAPL", "2026-07-24", 1.0, trigger_id="t")
    store.write("price.close", "MSFT", "2026-07-24", 2.0, trigger_id="t")
    panel = store.read_panel("price.close", "2026-07-24")
    assert set(panel) == {"AAPL", "MSFT"}
