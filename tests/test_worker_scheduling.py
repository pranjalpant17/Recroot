"""
Tests for the scheduler wired into the real worker pool (modules/worker.py).

No database is used. FakePool subclasses the real ResumeWorkerPool and
replaces only the two methods that touch MySQL:
    _lookup_task_metadata   -> returns scheduling data from a dict
    _process_with_retries   -> records the order and optionally waits on a gate
Everything else is the real code under test: enqueue, the scheduler queue,
worker threads, dispatch / completion numbering, stats, stop().

Pattern used to build a backlog deterministically with ONE worker: enqueue a
"blocker" task, wait until the worker is holding it (blocked on the gate),
enqueue the real tasks (they pile up in the queue), then open the gate.
"""

import threading
import time
import unittest
from datetime import date, datetime, timedelta

from modules import worker as worker_module
from modules.scheduler import ALGORITHM_FCFS, ALGORITHM_PRIORITY
from modules.scheduler_view import build_scheduler_view
from modules.worker import STATE_DONE, ResumeWorkerPool

TODAY = date.today()
T0 = datetime(2026, 1, 1, 9, 0, 0)


def meta(minutes, deadline_offset=None, title="Job"):
    """Scheduling data: applied `minutes` after T0; deadline = today + offset days."""
    return {
        "applied_at": T0 + timedelta(minutes=minutes),
        "deadline": None if deadline_offset is None else TODAY + timedelta(days=deadline_offset),
        "job_title": title,
    }


class FakePool(ResumeWorkerPool):
    def __init__(self, metas, gate=True, delays=None, **kwargs):
        kwargs.setdefault("db_config", {})
        kwargs.setdefault("upload_folder", ".")
        super().__init__(**kwargs)
        self.metas = metas
        self.fail_lookup = set()
        self.gate = threading.Event()
        if not gate:
            self.gate.set()
        self.delays = delays or {}
        self.started = []              # application ids in the order processing began
        self._rec = threading.Lock()

    def _lookup_task_metadata(self, application_id):
        if application_id in self.fail_lookup:
            raise RuntimeError("database unavailable")
        return self.metas.get(application_id)

    def _process_with_retries(self, application_id):
        with self._rec:
            self.started.append(application_id)
        self.gate.wait(10)
        time.sleep(self.delays.get(application_id, 0))
        return STATE_DONE, None


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


BLOCKER = 9999


def run_backlog(metas, order_of_arrival, algorithm, switch_to=None):
    """One worker, backlog built behind a blocker. Returns ids in the order
    the worker started them (blocker excluded)."""
    metas = dict(metas)
    metas[BLOCKER] = meta(-10_000)
    pool = FakePool(metas, pool_size=1, scheduling_algorithm=algorithm)
    pool.start()
    try:
        assert pool.enqueue(BLOCKER)
        assert wait_until(lambda: BLOCKER in pool.started)
        for app_id in order_of_arrival:
            assert pool.enqueue(app_id), app_id
        if switch_to:
            pool.set_scheduling_algorithm(switch_to)
        pool.gate.set()
        n = len(order_of_arrival) + 1
        assert wait_until(lambda: pool.get_stats()["completed"] == n)
        return [i for i in pool.started if i != BLOCKER]
    finally:
        pool.stop(timeout=2)


# Applications: id -> (minutes after T0, deadline offset in days)
MIXED = {
    1: meta(1, None),     # no deadline, earliest
    2: meta(2, 5),        # upcoming, in 5 days
    3: meta(3, -2),       # expired
    4: meta(4, 0),        # deadline today -> upcoming
    5: meta(5, 1),        # tomorrow
    6: meta(6, 5),        # same deadline as 2, applied later
    7: meta(7, None),     # no deadline
    8: meta(0, -9),       # expired, applied FIRST of all
}
ARRIVAL = [3, 1, 6, 5, 8, 2, 7, 4]    # deliberately not in applied_at order


class TestFcfsInWorker(unittest.TestCase):
    def test_fcfs_orders_by_applied_at_then_id(self):
        got = run_backlog(MIXED, ARRIVAL, ALGORITHM_FCFS)
        self.assertEqual(got, [8, 1, 2, 3, 4, 5, 6, 7])

    def test_fcfs_same_applied_at_uses_id(self):
        same = {20: meta(5), 10: meta(5), 30: meta(5), 5: meta(5)}
        got = run_backlog(same, [30, 10, 20, 5], ALGORITHM_FCFS)
        self.assertEqual(got, [5, 10, 20, 30])

    def test_fcfs_ignores_deadlines(self):
        m = {1: meta(1, 30), 2: meta(2, 0), 3: meta(3, -1)}
        self.assertEqual(run_backlog(m, [3, 2, 1], ALGORITHM_FCFS), [1, 2, 3])


class TestPriorityInWorker(unittest.TestCase):
    def test_priority_order_all_categories(self):
        got = run_backlog(MIXED, ARRIVAL, ALGORITHM_PRIORITY)
        # upcoming: today(4), tomorrow(5), +5 days (2 before 6 by applied_at);
        # then no deadline (1, 7); then expired by applied_at (8, 3)
        self.assertEqual(got, [4, 5, 2, 6, 1, 7, 8, 3])

    def test_deadline_today_is_upcoming_not_expired(self):
        m = {1: meta(1, -1), 2: meta(2, 0), 3: meta(3, None)}
        self.assertEqual(run_backlog(m, [1, 3, 2], ALGORITHM_PRIORITY), [2, 3, 1])

    def test_equal_deadlines_break_ties_by_applied_at_then_id(self):
        m = {7: meta(5, 3), 3: meta(5, 3), 9: meta(1, 3), 4: meta(0, 4)}
        self.assertEqual(run_backlog(m, [7, 4, 3, 9], ALGORITHM_PRIORITY), [9, 3, 7, 4])

    def test_no_deadline_and_expired_groups_use_applied_at_then_id(self):
        m = {1: meta(9, None), 2: meta(3, None), 3: meta(3, None),
             4: meta(8, -5), 5: meta(2, -1), 6: meta(2, -1)}
        got = run_backlog(m, [1, 4, 6, 3, 5, 2], ALGORITHM_PRIORITY)
        self.assertEqual(got, [2, 3, 1, 5, 6, 4])

    def test_expired_applications_are_never_dropped(self):
        m = {i: meta(i, -i) for i in range(1, 6)}
        got = run_backlog(m, [5, 4, 3, 2, 1], ALGORITHM_PRIORITY)
        self.assertEqual(sorted(got), [1, 2, 3, 4, 5])


class TestSwitchingAlgorithm(unittest.TestCase):
    def test_switch_fcfs_to_priority_reorders_waiting_tasks(self):
        got = run_backlog(MIXED, ARRIVAL, ALGORITHM_FCFS, switch_to=ALGORITHM_PRIORITY)
        self.assertEqual(got, [4, 5, 2, 6, 1, 7, 8, 3])

    def test_switch_priority_to_fcfs_reorders_waiting_tasks(self):
        got = run_backlog(MIXED, ARRIVAL, ALGORITHM_PRIORITY, switch_to=ALGORITHM_FCFS)
        self.assertEqual(got, [8, 1, 2, 3, 4, 5, 6, 7])

    def test_switch_does_not_affect_task_already_taken(self):
        pool = FakePool({1: meta(1), 2: meta(2)}, pool_size=1)
        pool.start()
        try:
            pool.enqueue(1)
            self.assertTrue(wait_until(lambda: pool.started == [1]))
            pool.set_scheduling_algorithm(ALGORITHM_PRIORITY)
            pool.gate.set()
            pool.enqueue(2)
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 2))
            self.assertEqual(pool.started, [1, 2])
        finally:
            pool.stop(timeout=2)

    def test_unknown_algorithm_rejected_and_setting_kept(self):
        pool = FakePool({})
        with self.assertRaises(ValueError):
            pool.set_scheduling_algorithm("round_robin")
        self.assertEqual(pool.get_scheduling_algorithm(), ALGORITHM_FCFS)

    def test_set_returns_previous_and_stats_report_active(self):
        pool = FakePool({})
        pool.start()
        try:
            self.assertEqual(pool.set_scheduling_algorithm(ALGORITHM_PRIORITY), ALGORITHM_FCFS)
            self.assertEqual(pool.get_stats()["scheduling_algorithm"], ALGORITHM_PRIORITY)
        finally:
            pool.stop(timeout=2)

    def test_default_algorithm_comes_from_constructor(self):
        self.assertEqual(FakePool({}, scheduling_algorithm=ALGORITHM_PRIORITY)
                         .get_scheduling_algorithm(), ALGORITHM_PRIORITY)


class TestQueueBehaviour(unittest.TestCase):
    def test_idle_worker_starts_new_task_immediately(self):
        pool = FakePool({1: meta(1)}, gate=False, pool_size=1)
        pool.start()
        try:
            pool.enqueue(1)
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 1))
        finally:
            pool.stop(timeout=2)

    def test_queue_full_is_rejected_without_blocking(self):
        pool = FakePool({i: meta(i) for i in range(1, 10)}, pool_size=1, queue_max_size=2)
        pool.start()
        try:
            pool.enqueue(1)
            self.assertTrue(wait_until(lambda: 1 in pool.started))
            self.assertTrue(pool.enqueue(2))
            self.assertTrue(pool.enqueue(3))
            self.assertFalse(pool.enqueue(4))          # full: rejected, returns at once
            self.assertEqual(pool.get_stats()["rejected_full"], 1)
            self.assertNotIn(4, pool._in_flight)       # can be recovered/retried later
            pool.gate.set()
        finally:
            pool.stop(timeout=2)

    def test_same_application_cannot_be_queued_twice(self):
        pool = FakePool({1: meta(1), 2: meta(2)}, pool_size=1)
        pool.start()
        try:
            pool.enqueue(1)
            self.assertTrue(wait_until(lambda: 1 in pool.started))
            self.assertTrue(pool.enqueue(2))
            self.assertFalse(pool.enqueue(2))
            self.assertFalse(pool.enqueue(1))          # still running
            self.assertEqual(pool.get_stats()["rejected_in_flight"], 2)
            pool.gate.set()
        finally:
            pool.stop(timeout=2)

    def test_application_can_be_requeued_after_it_finishes(self):
        pool = FakePool({1: meta(1)}, gate=False, pool_size=1)
        pool.start()
        try:
            pool.enqueue(1)
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 1))
            self.assertTrue(wait_until(lambda: pool.enqueue(1)))
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 2))
        finally:
            pool.stop(timeout=2)

    def test_not_started_pool_rejects_enqueue(self):
        self.assertFalse(FakePool({1: meta(1)}).enqueue(1))

    def test_stop_discards_waiting_tasks_and_marks_them(self):
        pool = FakePool({i: meta(i) for i in (1, 2, 3)}, pool_size=1)
        pool.start()
        pool.enqueue(1)
        self.assertTrue(wait_until(lambda: 1 in pool.started))
        pool.enqueue(2)
        pool.enqueue(3)
        pool.gate.set()
        pool.stop(timeout=0.01)      # waiting tasks 2 and 3 may or may not have started
        states = {i: (pool.get_task_status(i) or {}).get("state") for i in (2, 3)}
        for state in states.values():
            self.assertIn(state, ("discarded", "done", "processing"))
        self.assertFalse(pool.enqueue(4))   # stopped pool takes nothing new

    def test_stop_with_drain_finishes_waiting_tasks(self):
        pool = FakePool({i: meta(i) for i in (1, 2, 3)}, gate=False, pool_size=1,
                        delays={1: 0.05, 2: 0.05, 3: 0.05})
        pool.start()
        for i in (1, 2, 3):
            pool.enqueue(i)
        pool.stop(drain=True, timeout=5)
        self.assertEqual(pool.get_stats()["completed"], 3)

    def test_queue_depth_counts_waiting_only(self):
        pool = FakePool({i: meta(i) for i in (1, 2, 3)}, pool_size=1)
        pool.start()
        try:
            pool.enqueue(1)
            self.assertTrue(wait_until(lambda: 1 in pool.started))
            pool.enqueue(2)
            pool.enqueue(3)
            self.assertEqual(pool.get_stats()["queue_depth"], 2)
            pool.gate.set()
        finally:
            pool.stop(timeout=2)

    def test_lookup_failure_still_schedules_and_flags_missing_metadata(self):
        pool = FakePool({BLOCKER: meta(0), 2: meta(2)}, pool_size=1)
        pool.fail_lookup = {1}
        pool.start()
        try:
            pool.enqueue(BLOCKER)
            self.assertTrue(wait_until(lambda: BLOCKER in pool.started))
            self.assertTrue(pool.enqueue(1))             # lookup raises; must not be lost
            row = pool.get_scheduler_snapshot()["tasks"][0]
            self.assertEqual(row["application_id"], 1)
            self.assertTrue(row["metadata_missing"])
            pool.gate.set()
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 2))
        finally:
            pool.stop(timeout=2)

    def test_task_without_database_row_is_still_scheduled(self):
        pool = FakePool({}, gate=False, pool_size=1)      # lookup returns None
        pool.start()
        try:
            self.assertTrue(pool.enqueue(42))
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 1))
        finally:
            pool.stop(timeout=2)

    def test_recovery_style_metadata_skips_lookup(self):
        pool = FakePool({}, pool_size=1)
        pool.fail_lookup = {1, 2}          # a lookup would raise
        pool.start()
        try:
            self.assertTrue(pool.enqueue(1, metadata=meta(1)))
            self.assertTrue(wait_until(lambda: 1 in pool.started))
            self.assertTrue(pool.enqueue(2, metadata=meta(2, 3)))
            row = pool.get_scheduler_snapshot()["tasks"][0]
            self.assertFalse(row["metadata_missing"])
            self.assertEqual(row["deadline"], TODAY + timedelta(days=3))
            pool.gate.set()
        finally:
            pool.stop(timeout=2)


class TestDisplayMatchesRealOrder(unittest.TestCase):
    def _check(self, algorithm):
        metas = dict(MIXED)
        metas[BLOCKER] = meta(-10_000)
        pool = FakePool(metas, pool_size=1, scheduling_algorithm=algorithm)
        pool.start()
        try:
            pool.enqueue(BLOCKER)
            self.assertTrue(wait_until(lambda: BLOCKER in pool.started))
            for app_id in ARRIVAL:
                pool.enqueue(app_id)
            shown = [r["application_id"] for r in
                     sorted(pool.get_scheduler_snapshot()["tasks"], key=lambda r: r["position"])]
            positions = [r["position"] for r in pool.get_scheduler_snapshot()["tasks"]]
            pool.gate.set()
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == len(ARRIVAL) + 1))
            actual = [i for i in pool.started if i != BLOCKER]
            self.assertEqual(shown, actual)            # the page is the real order
            self.assertEqual(sorted(positions), list(range(1, len(ARRIVAL) + 1)))
        finally:
            pool.stop(timeout=2)

    def test_fcfs_display_equals_dispatch_order(self):
        self._check(ALGORITHM_FCFS)

    def test_priority_display_equals_dispatch_order(self):
        self._check(ALGORITHM_PRIORITY)


class TestDispatchVersusCompletion(unittest.TestCase):
    def test_sequences_are_unique_and_completion_can_differ_from_dispatch(self):
        # Two workers. App 1 is dispatched first but is the slowest, so app 2
        # finishes before it: completion order != dispatch order.
        pool = FakePool({1: meta(1), 2: meta(2)}, gate=False, pool_size=2, delays={1: 0.3, 2: 0.0})
        pool.start()
        try:
            pool.enqueue(1)
            self.assertTrue(wait_until(lambda: 1 in pool.started))
            pool.enqueue(2)
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 2))
            e1, e2 = pool.get_task_status(1), pool.get_task_status(2)
            self.assertEqual((e1["dispatch_seq"], e2["dispatch_seq"]), (1, 2))
            self.assertEqual((e1["completion_seq"], e2["completion_seq"]), (2, 1))
            self.assertNotEqual(e1["worker"], e2["worker"])
        finally:
            pool.stop(timeout=2)

    def test_dispatched_rows_appear_in_snapshot_in_dispatch_order(self):
        got = []
        pool = FakePool({i: meta(i) for i in (1, 2, 3)}, gate=False, pool_size=1)
        pool.start()
        try:
            for i in (1, 2, 3):
                pool.enqueue(i)
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 3))
            rows = pool.get_scheduler_snapshot()["dispatched"]
            self.assertEqual([r["dispatch_seq"] for r in rows], [1, 2, 3])
            self.assertTrue(all(r["worker"].startswith("recroot-worker-") for r in rows))
        finally:
            pool.stop(timeout=2)

    def test_many_workers_never_run_same_application_twice(self):
        n = 40
        pool = FakePool({i: meta(i) for i in range(1, n + 1)}, gate=False, pool_size=4,
                        queue_max_size=100, delays={i: 0.001 for i in range(1, n + 1)})
        pool.start()
        try:
            for i in range(1, n + 1):
                pool.enqueue(i)
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == n, 10))
            self.assertEqual(sorted(pool.started), list(range(1, n + 1)))
            seqs = sorted(pool.get_task_status(i)["dispatch_seq"] for i in range(1, n + 1))
            self.assertEqual(seqs, list(range(1, n + 1)))
        finally:
            pool.stop(timeout=2)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.conn.executed.append((" ".join(sql.split()), tuple(params)))
        self._rows = list(self.conn.rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []
        self.rolled_back = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


class SqlPool(ResumeWorkerPool):
    """Real SQL-issuing methods, fake connection; processing is stubbed."""

    def __init__(self, rows, **kwargs):
        super().__init__(db_config={}, upload_folder=".", **kwargs)
        self.conn = FakeConn(rows)
        self.gate = threading.Event()
        self.started = []

    def _open_connection(self):
        return self.conn

    def _process_with_retries(self, application_id):
        self.started.append(application_id)
        self.gate.wait(10)
        return STATE_DONE, None


class TestDatabaseBackedPaths(unittest.TestCase):
    def test_metadata_lookup_reads_applied_at_deadline_and_title(self):
        row = {"applied_at": T0, "deadline": TODAY, "job_title": "Backend Developer"}
        pool = SqlPool([row])
        self.assertEqual(pool._lookup_task_metadata(7), row)
        sql, params = pool.conn.executed[0]
        self.assertIn("a.applied_at", sql)
        self.assertIn("j.deadline", sql)
        self.assertEqual(params, (7,))
        self.assertTrue(sql.startswith("SELECT"))          # read only
        self.assertEqual(pool.conn.rolled_back, 1)
        self.assertTrue(pool.conn.closed)

    def test_metadata_lookup_missing_row_returns_none(self):
        self.assertIsNone(SqlPool([])._lookup_task_metadata(7))

    def test_enqueue_uses_database_metadata(self):
        row = {"applied_at": T0, "deadline": TODAY + timedelta(days=2), "job_title": "QA"}
        pool = SqlPool([row], pool_size=1)
        pool.start()
        try:
            pool.enqueue(BLOCKER)
            self.assertTrue(wait_until(lambda: BLOCKER in pool.started))
            pool.enqueue(5)
            task_row = pool.get_scheduler_snapshot()["tasks"][0]
            self.assertEqual(task_row["job_title"], "QA")
            self.assertEqual(task_row["deadline"], TODAY + timedelta(days=2))
            pool.gate.set()
        finally:
            pool.stop(timeout=2)

    def test_recovery_sweep_feeds_scheduler_with_metadata_and_respects_algorithm(self):
        rows = [
            {"id": 1, "applied_at": T0 + timedelta(minutes=1), "deadline": None, "job_title": "A"},
            {"id": 2, "applied_at": T0 + timedelta(minutes=2), "deadline": TODAY + timedelta(days=9), "job_title": "B"},
            {"id": 3, "applied_at": T0 + timedelta(minutes=3), "deadline": TODAY - timedelta(days=1), "job_title": "C"},
            {"id": 4, "applied_at": T0 + timedelta(minutes=4), "deadline": TODAY, "job_title": "D"},
        ]
        pool = SqlPool(rows, pool_size=1, scheduling_algorithm=ALGORITHM_PRIORITY)
        pool.start()
        try:
            pool.enqueue(BLOCKER)
            self.assertTrue(wait_until(lambda: BLOCKER in pool.started))
            summary = pool.recover_unprocessed()
            self.assertEqual(summary["enqueued"], 4)
            sweep_sql = [s for s, _ in pool.conn.executed if "processing_status IN" in s][0]
            self.assertIn("ORDER BY a.applied_at, a.id", sweep_sql)
            self.assertIn("LEFT JOIN job_postings", sweep_sql)
            pool.gate.set()
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 5))
            self.assertEqual([i for i in pool.started if i != BLOCKER], [4, 2, 1, 3])
        finally:
            pool.stop(timeout=2)

    def test_recovery_sweep_leaves_remainder_when_queue_full(self):
        rows = [{"id": i, "applied_at": T0 + timedelta(minutes=i), "deadline": None,
                 "job_title": "X"} for i in range(1, 6)]
        pool = SqlPool(rows, pool_size=1, queue_max_size=2)
        pool.start()
        try:
            pool.enqueue(BLOCKER)
            self.assertTrue(wait_until(lambda: BLOCKER in pool.started))
            summary = pool.recover_unprocessed()
            self.assertEqual(summary["enqueued"], 2)
            self.assertEqual(summary["left_for_later"], 3)
            pool.gate.set()
        finally:
            pool.stop(timeout=2)


class TestModuleLevelApi(unittest.TestCase):
    def setUp(self):
        worker_module.shutdown_worker()

    def test_no_pool_means_none_or_runtime_error(self):
        self.assertIsNone(worker_module.get_scheduler_snapshot())
        self.assertIsNone(worker_module.get_scheduling_algorithm())
        with self.assertRaises(RuntimeError):
            worker_module.set_scheduling_algorithm("priority")

    def test_enqueue_without_pool_returns_false(self):
        self.assertFalse(worker_module.enqueue_application(1))

    def test_module_functions_drive_the_live_pool(self):
        pool = FakePool({})
        worker_module._pool = pool
        try:
            self.assertEqual(worker_module.get_scheduling_algorithm(), ALGORITHM_FCFS)
            worker_module.set_scheduling_algorithm("priority")
            self.assertEqual(worker_module.get_scheduling_algorithm(), ALGORITHM_PRIORITY)
            self.assertEqual(worker_module.get_scheduler_snapshot()["algorithm"], "priority")
        finally:
            worker_module._pool = None


class TestSchedulerViewModel(unittest.TestCase):
    def test_unavailable_when_no_pool(self):
        view = build_scheduler_view(None)
        self.assertFalse(view["available"])
        self.assertEqual([a for a, _ in view["algorithms"]], ["fcfs", "priority"])

    def test_rows_sorted_by_position(self):
        pool = FakePool({**MIXED, BLOCKER: meta(-5)}, pool_size=1,
                        scheduling_algorithm=ALGORITHM_PRIORITY)
        pool.start()
        try:
            pool.enqueue(BLOCKER)
            self.assertTrue(wait_until(lambda: BLOCKER in pool.started))
            for i in ARRIVAL:
                pool.enqueue(i)
            view = build_scheduler_view(pool.get_scheduler_snapshot())
            self.assertTrue(view["available"])
            self.assertEqual(view["algorithm"], "priority")
            self.assertEqual([r["position"] for r in view["waiting"]], list(range(1, 9)))
            self.assertEqual([r["application_id"] for r in view["waiting"]],
                             [4, 5, 2, 6, 1, 7, 8, 3])
            self.assertEqual(view["waiting"][0]["bucket_label"], "upcoming")
            self.assertEqual(view["waiting"][-1]["bucket_label"], "expired")
            pool.gate.set()
        finally:
            pool.stop(timeout=2)


if __name__ == "__main__":
    unittest.main()
