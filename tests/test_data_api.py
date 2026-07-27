"""Gated data-retrieval API contract tests — the guarantees DataAPI promises:
the DB is reachable only through a read-only, validated, parameterized API.
"""
import sqlite3
import stat

import pytest

from core import (DataAPI, FeatureStore, DataAPIError, UnknownSignalError,
                  UnknownScopeError, InvalidTimeRangeError)


@pytest.fixture
def db(tmp_path):
    """A store seeded with two registered signals across two tickers + market."""
    path = tmp_path / "features.db"
    s = FeatureStore(path)
    s.register("price.close", "float", "ticker", "S1", "daily", "post-close")
    s.register("alpha.regime", "str", "market", "S4", "daily", "eod")
    s.write("price.close", "AAPL", "2026-07-21", 100.0, trigger_id="t")
    s.write("price.close", "AAPL", "2026-07-22", 101.0, trigger_id="t")
    s.write("price.close", "AAPL", "2026-07-23", 102.0, trigger_id="t")
    s.write("price.close", "MSFT", "2026-07-22", 390.0, trigger_id="t")
    s.write("alpha.regime", "_market", "2026-07-22", "risk_on", trigger_id="t")
    s.close()
    return path


# ── the happy path ────────────────────────────────────────────────────────────

def test_get_returns_ascending_window(db):
    api = DataAPI(db)
    rows = api.get("AAPL", "price.close", "2026-07-21", "2026-07-23")
    assert [r["value"] for r in rows] == [100.0, 101.0, 102.0]
    assert [r["event_time"] for r in rows] == ["2026-07-21", "2026-07-22", "2026-07-23"]


def test_get_respects_time_bounds(db):
    api = DataAPI(db)
    rows = api.get("AAPL", "price.close", "2026-07-22", "2026-07-22")
    assert [r["value"] for r in rows] == [101.0]


def test_date_only_end_is_inclusive_of_intraday(tmp_path):
    """A date-only end bound must include that day's intraday timestamps."""
    path = tmp_path / "f.db"
    s = FeatureStore(path)
    s.register("q.mid", "float", "ticker", "S1", "5min", "live")
    s.write("q.mid", "AAPL", "2026-07-22T15:30:00+00:00", 1.0, trigger_id="t")
    s.write("q.mid", "AAPL", "2026-07-22T20:00:00+00:00", 2.0, trigger_id="t")
    s.close()
    rows = DataAPI(path).get("AAPL", "q.mid", "2026-07-22", "2026-07-22")
    assert [r["value"] for r in rows] == [1.0, 2.0]


def test_market_scope_signal(db):
    rows = DataAPI(db).get("_market", "alpha.regime", "2026-07-01", "2026-07-31")
    assert rows[-1]["value"] == "risk_on"


# ── validation / rejection ──────────────────────────────────────────────────────

def test_unknown_signal_rejected(db):
    with pytest.raises(UnknownSignalError):
        DataAPI(db).get("AAPL", "not.a.signal", "2026-07-21", "2026-07-23")


def test_unknown_ticker_rejected(db):
    with pytest.raises(UnknownScopeError):
        DataAPI(db).get("NOPE", "price.close", "2026-07-21", "2026-07-23")


def test_injection_attempt_is_rejected_not_executed(db):
    """A SQL-injection-shaped ticker fails validation — it is never interpolated."""
    with pytest.raises(UnknownScopeError):
        DataAPI(db).get("AAPL'; DROP TABLE feature_values;--", "price.close",
                        "2026-07-21", "2026-07-23")
    # table still intact
    assert DataAPI(db).get("AAPL", "price.close", "2026-07-21", "2026-07-23")


def test_reversed_range_rejected(db):
    with pytest.raises(InvalidTimeRangeError):
        DataAPI(db).get("AAPL", "price.close", "2026-07-23", "2026-07-21")


def test_unparseable_time_rejected(db):
    with pytest.raises(InvalidTimeRangeError):
        DataAPI(db).get("AAPL", "price.close", "last-tuesday", "2026-07-23")


# ── read-only enforcement + file lock ───────────────────────────────────────────

def test_handle_is_read_only(db):
    api = DataAPI(db)
    with pytest.raises(sqlite3.OperationalError):
        api._conn.execute("INSERT INTO feature_values VALUES ('x','Y','t','i','1','t')")


def test_db_file_is_owner_only_after_open(db):
    DataAPI(db)  # hardens on construction
    mode = stat.S_IMODE(db.stat().st_mode)
    assert mode & 0o077 == 0, f"expected owner-only, got {oct(mode)}"


def test_missing_db_raises(tmp_path):
    with pytest.raises(DataAPIError):
        DataAPI(tmp_path / "does-not-exist.db")


# ── point-in-time / bitemporal correctness ──────────────────────────────────────

def test_as_known_at_hides_later_corrections(tmp_path):
    path = tmp_path / "f.db"
    s = FeatureStore(path)
    s.register("price.close", "float", "ticker", "S1", "daily", "post-close")
    s.write("price.close", "AAPL", "2026-07-22", 100.0, trigger_id="t1",
            ingested_at="2026-07-22T21:00:00+00:00")
    s.write("price.close", "AAPL", "2026-07-22", 99.0, trigger_id="t2",   # correction later
            ingested_at="2026-07-24T09:00:00+00:00")
    s.close()
    api = DataAPI(path)
    known_early = api.get("AAPL", "price.close", "2026-07-22", "2026-07-22",
                          as_known_at="2026-07-23T00:00:00+00:00")
    assert known_early[0]["value"] == 100.0     # correction invisible at that time
    known_now = api.get("AAPL", "price.close", "2026-07-22", "2026-07-22")
    assert known_now[0]["value"] == 99.0        # latest version otherwise


def test_latest_convenience(db):
    assert DataAPI(db).latest("AAPL", "price.close")["value"] == 102.0


def test_signals_and_scopes_discovery(db):
    api = DataAPI(db)
    assert "price.close" in api.signals()
    assert set(api.scopes("price.close")) == {"AAPL", "MSFT"}
