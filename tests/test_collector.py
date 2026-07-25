"""Collector tests — the queue/scheduler/rate-limiter logic, fully offline with a
fake clock and fake handlers (no network)."""
from datetime import datetime, timezone, timedelta

import pytest

import core as core_mod
from core import FeatureStore
from collector import Collector


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def advance(self, sec): self.t += timedelta(seconds=sec)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(core_mod, "LOGS_DIR", tmp_path / "logs")
    yield


@pytest.fixture
def col(tmp_path):
    clock = Clock(datetime(2026, 7, 1, tzinfo=timezone.utc))
    store = FeatureStore(tmp_path / "f.db")
    store.register("test.value", "float", "ticker", "S1", "daily", "pit")
    c = Collector(tmp_path / "f.db", store=store, now_fn=clock)
    c.clock = clock
    c.calls = []

    def handler(scope, store, tid):
        c.calls.append(scope)
        store.write("test.value", scope, "2026-07-01", 1.0, trigger_id=tid)
        return 1, 1
    c.register_source("fake", limit=2, window_sec=60)
    c.register_kind("val", "fake", interval_sec=3600, priority=20, handler=handler)
    return c


def test_seed_enqueues_and_tick_runs_due_task(col):
    col.seed(["AAPL", "MSFT"])
    r = col.tick()
    assert r["rows"] == 1 and r["task"].startswith("fake:val:")
    # the run wrote to the store
    assert col.store.read_asof("test.value", col.calls[0], "2026-07-01")["value"] == 1.0


def test_recurring_reschedules_by_interval(col):
    col.seed(["AAPL"])
    col.tick()                                   # runs AAPL, reschedules +3600s
    assert col.tick() is None                    # nothing due now
    col.clock.advance(3601)
    assert col.tick() is not None                # due again after the interval


def test_rate_limit_is_strict(col):
    col.seed(["AAPL", "MSFT", "NVDA"])           # 3 due tasks, limit 2/60s
    assert col.tick() is not None
    assert col.tick() is not None
    assert col.available("fake") == 0
    assert col.tick() is None                    # 3rd blocked by quota
    col.clock.advance(61)
    assert col.available("fake") == 2
    assert col.tick() is not None                # quota refilled


def test_add_ticker_backfill_jumps_the_queue(col):
    col.seed(["AAPL"])                           # priority 20, due now
    col.clock.advance(10)
    col.add_ticker("ZZZZ")                       # boosted priority (20-100), due now
    r = col.tick()
    assert r["task"] == "fake:val:ZZZZ"          # backfill runs before the routine AAPL


def test_handler_failure_backs_off(col):
    def boom(scope, store, tid):
        raise RuntimeError("kaboom")
    col.register_kind("bad", "fake", interval_sec=3600, priority=5, handler=boom)
    col._upsert("fake", "bad", "AAPL", 5, 3600, col.clock().isoformat())
    r = col.tick()
    assert "error" in r
    row = col.c.execute("SELECT attempts, next_due, last_error FROM collection_tasks "
                        "WHERE task_id='fake:bad:AAPL'").fetchone()
    assert row[0] == 1 and "kaboom" in row[2]
    # backed off into the future -> not due now
    assert datetime.fromisoformat(row[1]) > col.clock()


def test_status_reports_quota_and_counts(col):
    col.seed(["AAPL", "MSFT"])
    col.tick()
    s = col.status()
    assert s["by_status"]["pending"] == 2
    assert "fake" in s["quota"]
