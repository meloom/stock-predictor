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


def test_saturated_source_does_not_starve_others(col):
    """Regression: a rate-limited source with MANY high-priority due tasks (more
    than any query LIMIT) must not starve a lower-priority task on a source that
    still has quota — the worker falls through to the source with capacity."""
    ran = []
    col.register_source("lim", limit=1, window_sec=60)
    col.register_kind("a", "lim", interval_sec=3600, priority=5,
                      handler=lambda s, st, t: (ran.append(("a", s)), (1, 1))[1])
    col.register_source("free", limit=50, window_sec=60)
    col.register_kind("b", "free", interval_sec=3600, priority=50,
                      handler=lambda s, st, t: (ran.append(("b", s)), (1, 1))[1])
    now = col.clock().isoformat()
    for i in range(150):                       # 150 high-priority rate-limited tasks
        col._upsert("lim", "a", f"T{i}", 5, 3600, now)
    col._upsert("free", "b", "Z", 50, 3600, now)   # one low-priority task with quota
    col._record_calls("lim", 1)                # exhaust the 'lim' source (limit 1)
    assert col.available("lim") == 0
    r = col.tick()                             # must run the 'free' task, not stall
    assert r is not None and r["task"] == "free:b:Z"
    assert ("b", "Z") in ran


def test_earnings_processing_depends_on_raw(col):
    """The processed earnings analysis must READ the downloaded raw record and
    write the derived outcome — and no-op (retry) when the raw isn't there yet."""
    import collector as C
    # analysis before any raw is downloaded -> no-op (0 rows, 0 calls)
    assert C._h_earn_analysis("AAPL", col.store, "t") == (0, 0)
    for n, dt, sk, cd, r in C._EARN_FEATURES:
        col.store.register(n, dt, sk, "S1", cd, r)
    # raw reports are per-quarter, this-quarter-only (NO revenue_year_ago field). YoY is
    # derived in S2 from the year-ago quarter's raw revenue, so we store BOTH quarters.
    ya = {"event_time": "2025-04-30", "revenue": 95.0, "net_income": 25.0}
    raw = {"event_time": "2026-04-30", "eps_estimate": 1.94, "eps_reported": 2.01,
           "surprise_pct": 3.46, "revenue": 111.0, "net_income": 29.0}
    col.store.write("earnings.report_raw", "AAPL", "2025-04-30", ya, trigger_id="t")
    col.store.write("earnings.report_raw", "AAPL", "2026-04-30", raw, trigger_id="t")
    # the S2 processing task derives YoY from the two raw quarters + stores the analysis
    assert C._h_earn_analysis("AAPL", col.store, "t") == (1, 0)
    ana = col.store.read_asof("earnings.analysis", "AAPL", "2026-04-30")["value"]
    assert ana["beat_miss"] == "beat" and ana["processed"] is True
    assert round(ana["revenue_yoy_pct"], 1) == 16.8      # (111-95)/95, computed in S2


def test_reconcile_auto_backfills_new_kind_as_prioritized(col):
    """Declaring a new kind + reconcile() must auto-enqueue a PRIORITIZED backfill
    task for it (no manual seed), at a priority band below every routine refresh."""
    col.register_kind("newsig", "fake", interval_sec=3600, priority=30,
                      handler=lambda s, st, t: (1, 1))
    armed = col.reconcile(tickers=["AAA", "BBB"])
    assert armed >= 2
    pr, st = col.c.execute("SELECT priority, status FROM collection_tasks "
                           "WHERE task_id='fake:newsig:AAA'").fetchone()
    assert pr == 30 - 1000 and st == "pending"          # backfill band, ready to run
    # idempotent: a second reconcile does not re-arm already-queued tasks
    assert col.reconcile(tickers=["AAA", "BBB"]) == 0


def test_backfill_priority_restores_to_base_after_success(col):
    """Once a backfill collects, the task drops back to its routine-refresh priority."""
    col.register_kind("newsig", "fake", interval_sec=3600, priority=30,
                      handler=lambda s, st, t: (1, 1))
    col._upsert("fake", "newsig", "AAA", 30 - 1000, 3600, col.clock().isoformat())
    col.tick()                                          # runs the boosted backfill
    p = col.c.execute("SELECT priority FROM collection_tasks "
                      "WHERE task_id='fake:newsig:AAA'").fetchone()[0]
    assert p == 30                                      # restored to routine refresh


def test_status_reports_quota_and_counts(col):
    col.seed(["AAPL", "MSFT"])
    col.tick()
    s = col.status()
    assert s["by_status"]["pending"] == 2
    assert "fake" in s["quota"]
