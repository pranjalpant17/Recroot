"""
Tests for the optional DEMO dispatch window (WORKER_DISPATCH_WINDOW_SECONDS).

The window uses real (small) time because it is a time feature; every
assertion has wide margins (window 0.5 s, submissions ~50 ms apart) so the
tests are not timing-sensitive. No database is used.
"""

import queue
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from modules import worker as worker_module
from modules.scheduler import (
    ALGORITHM_FCFS, ALGORITHM_PRIORITY, MODE_MANUAL, SchedulerQueue,
    make_task, normalize_dispatch_window,
)
from tests.test_admin_scheduler_page import login, make_app
from tests.test_worker_scheduling import FakePool, meta, wait_until

TODAY = date(2026, 10, 27)
T0 = datetime(2026, 10, 27, 15, 50, 0)
WINDOW = 0.5


def task(app_id, minutes, deadline_days=None):
    deadline = None if deadline_days is None else TODAY + timedelta(days=deadline_days)
    return make_task(app_id, applied_at=T0 + timedelta(minutes=minutes), deadline=deadline)


def make_queue(algorithm, window=WINDOW, **kwargs):
    return SchedulerQueue(capacity=50, algorithm=algorithm, today_func=lambda: TODAY,
                          dispatch_window_seconds=window, **kwargs)


def consume(q, n, out, times):
    """Thread body: pop n tasks, recording ids and the monotonic time of each."""
    for _ in range(n):
        decision = q.pop_next()
        if decision is None:
            return
        out.append(decision.task.application_id)
        times.append(time.monotonic())


class TestNormalizeWindow(unittest.TestCase):
    def test_accepts_numbers_and_numeric_strings(self):
        self.assertEqual(normalize_dispatch_window(None), 0.0)
        self.assertEqual(normalize_dispatch_window(0), 0.0)
        self.assertEqual(normalize_dispatch_window("5"), 5.0)
        self.assertEqual(normalize_dispatch_window(" 2.5 "), 2.5)
        self.assertEqual(normalize_dispatch_window(60), 60.0)

    def test_rejects_bad_values(self):
        for bad in (-1, "-0.1", "abc", "", True, float("nan"), float("inf"), 61, "3600", [5]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_dispatch_window(bad)

    def test_queue_and_pool_reject_bad_values_at_construction(self):
        with self.assertRaises(ValueError):
            SchedulerQueue(dispatch_window_seconds=-5)
        with self.assertRaises(ValueError):
            FakePool({}, dispatch_window_seconds="soon")


class TestWindowDisabledByDefault(unittest.TestCase):
    def test_default_is_zero_and_pop_is_immediate(self):
        q = SchedulerQueue(capacity=5, today_func=lambda: TODAY)
        self.assertEqual(q.get_dispatch_window(), 0.0)
        q.add(task(1, 1))
        began = time.monotonic()
        self.assertEqual(q.pop_next(timeout=1).task.application_id, 1)
        self.assertLess(time.monotonic() - began, 0.2)

    def test_snapshot_reports_disabled(self):
        snap = SchedulerQueue(capacity=5).snapshot()
        self.assertEqual(snap["dispatch_window_seconds"], 0.0)
        self.assertEqual(snap["dispatch_window_remaining"], 0.0)

    def test_pool_default_is_disabled(self):
        self.assertEqual(FakePool({}).get_stats()["dispatch_window_seconds"], 0.0)


class TestWindowBehaviourInQueue(unittest.TestCase):
    def test_nothing_is_selected_while_window_is_open(self):
        q = make_queue(ALGORITHM_FCFS)
        q.add(task(1, 1))
        with self.assertRaises(queue.Empty):
            q.pop_next(timeout=0.15)                 # window (0.5 s) still open
        self.assertEqual(q.pending_count(), 1)       # task kept, not lost

    def test_snapshot_shows_window_open_then_closed(self):
        q = make_queue(ALGORITHM_FCFS)
        q.add(task(1, 1))
        snap = q.snapshot()
        self.assertEqual(snap["dispatch_window_seconds"], WINDOW)
        self.assertGreater(snap["dispatch_window_remaining"], 0.2)
        time.sleep(WINDOW + 0.1)
        self.assertEqual(q.snapshot()["dispatch_window_remaining"], 0.0)

    def _run(self, algorithm, tasks, spacing=0.05):
        q = make_queue(algorithm)
        got, times = [], []
        worker = threading.Thread(target=consume, args=(q, len(tasks), got, times))
        worker.start()                               # idle worker already waiting
        began = time.monotonic()
        for t in tasks:
            q.add(t)
            time.sleep(spacing)                      # submissions a moment apart
        worker.join(5)
        self.assertFalse(worker.is_alive())
        return got, [x - began for x in times]

    def test_priority_orders_tasks_that_arrive_a_moment_apart(self):
        # screenshot data: #54 +1d, #55 +8d, #56 +2d, submitted in that order
        got, times = self._run(ALGORITHM_PRIORITY,
                               [task(54, 0, 1), task(55, 1, 8), task(56, 2, 2)])
        self.assertEqual(got, [54, 56, 55])
        self.assertGreaterEqual(times[0], WINDOW - 0.05)   # first waited out the window

    def test_priority_full_rule_with_window(self):
        tasks = [task(1, 1, None), task(2, 2, -3), task(3, 3, 5), task(4, 4, 0),
                 task(5, 5, None), task(6, 6, -9)]
        got, _ = self._run(ALGORITHM_PRIORITY, tasks, spacing=0.03)
        # today(4), +5d(3), no-deadline by applied_at (1,5), expired by applied_at (2,6)
        self.assertEqual(got, [4, 3, 1, 5, 2, 6])

    def test_priority_tiebreakers_with_window(self):
        tasks = [task(9, 5, 3), task(7, 5, 3), task(8, 1, 3), task(2, 0, 4)]
        got, _ = self._run(ALGORITHM_PRIORITY, tasks, spacing=0.03)
        self.assertEqual(got, [8, 7, 9, 2])

    def test_fcfs_stays_in_arrival_order_with_window(self):
        # applied_at order differs from the submission order on purpose
        got, times = self._run(ALGORITHM_FCFS,
                               [task(3, 3, 1), task(1, 1, 9), task(2, 2, 2)])
        self.assertEqual(got, [1, 2, 3])
        self.assertGreaterEqual(times[0], WINDOW - 0.05)

    def test_window_is_fixed_later_arrivals_do_not_extend_it(self):
        q = make_queue(ALGORITHM_FCFS)
        got, times = [], []
        worker = threading.Thread(target=consume, args=(q, 1, got, times))
        worker.start()
        began = time.monotonic()
        q.add(task(1, 1))
        time.sleep(0.3)
        q.add(task(2, 2))                            # arrives mid-window
        worker.join(5)
        self.assertEqual(got, [1])
        self.assertLess(times[0] - began, WINDOW + 0.25)   # not 0.3 + 0.5

    def test_burst_drains_without_a_new_window_per_task(self):
        q = make_queue(ALGORITHM_FCFS)
        for i in (1, 2, 3, 4):
            q.add(task(i, i))
        time.sleep(WINDOW + 0.05)
        began = time.monotonic()
        order = [q.pop_next(timeout=1).task.application_id for _ in range(4)]
        self.assertEqual(order, [1, 2, 3, 4])
        self.assertLess(time.monotonic() - began, 0.2)      # no 0.5 s wait per task

    def test_new_window_opens_after_queue_empties(self):
        q = make_queue(ALGORITHM_FCFS, window=0.3)
        q.add(task(1, 1))
        q.pop_next(timeout=2)
        self.assertEqual(q.pending_count(), 0)
        q.add(task(2, 2))
        with self.assertRaises(queue.Empty):
            q.pop_next(timeout=0.1)                  # window open again
        self.assertEqual(q.pop_next(timeout=2).task.application_id, 2)

    def test_close_wakes_a_worker_waiting_out_the_window(self):
        q = make_queue(ALGORITHM_FCFS, window=30)
        q.add(task(1, 1))
        result = []
        thread = threading.Thread(target=lambda: result.append(q.pop_next()))
        thread.start()
        time.sleep(0.1)
        q.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [None])

    def test_manual_mode_run_is_not_delayed_by_the_window(self):
        q = make_queue(ALGORITHM_PRIORITY, window=30, mode=MODE_MANUAL)
        q.add(task(1, 1, 5))
        q.add(task(2, 2, 1))
        q.run_scheduler()
        began = time.monotonic()
        self.assertEqual(q.pop_next(timeout=1).task.application_id, 2)
        self.assertLess(time.monotonic() - began, 0.2)

    def test_algorithm_switch_during_window_applies_when_it_closes(self):
        q = make_queue(ALGORITHM_FCFS)
        for t in (task(1, 1, 9), task(2, 2, 1)):
            q.add(t)
        q.set_algorithm(ALGORITHM_PRIORITY)
        self.assertEqual(q.pop_next(timeout=2).task.application_id, 2)


class TestWindowInRealWorker(unittest.TestCase):
    """Screenshot scenario through the real worker with ONE idle worker."""

    METAS = {54: meta(0, 1), 55: meta(1, 8), 56: meta(2, 2)}

    def run_pool(self, algorithm, order=(54, 55, 56), metas=None, window=WINDOW):
        pool = FakePool(metas or self.METAS, gate=False, pool_size=1,
                        scheduling_algorithm=algorithm, dispatch_window_seconds=window)
        pool.start()
        try:
            began = time.monotonic()
            for app_id in order:
                pool.enqueue(app_id)
                time.sleep(0.05)
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == len(order), 5))
            return list(pool.started), began, pool
        finally:
            pool.stop(timeout=2)

    def test_priority_with_window_dispatches_54_56_55(self):
        started, began, pool = self.run_pool(ALGORITHM_PRIORITY)
        self.assertEqual(started, [54, 56, 55])
        self.assertEqual(pool.get_stats()["dispatch_window_seconds"], WINDOW)

    def test_fcfs_with_window_dispatches_54_55_56(self):
        started, _, _ = self.run_pool(ALGORITHM_FCFS)
        self.assertEqual(started, [54, 55, 56])

    def test_priority_with_window_full_rule(self):
        metas = {1: meta(1, None), 2: meta(2, -3), 3: meta(3, 5), 4: meta(4, 0),
                 5: meta(5, None), 6: meta(6, -9)}
        started, _, _ = self.run_pool(ALGORITHM_PRIORITY, order=(1, 2, 3, 4, 5, 6), metas=metas)
        self.assertEqual(started, [4, 3, 1, 5, 2, 6])

    def test_first_task_waits_for_the_window(self):
        pool = FakePool(self.METAS, gate=False, pool_size=1, dispatch_window_seconds=WINDOW)
        pool.start()
        try:
            began = time.monotonic()
            pool.enqueue(54)
            self.assertTrue(wait_until(lambda: pool.started == [54], 5))
            self.assertGreaterEqual(time.monotonic() - began, WINDOW - 0.05)
        finally:
            pool.stop(timeout=2)

    def test_without_window_same_submissions_stay_in_arrival_order(self):
        """The reported behaviour, still the default: window off -> no reordering."""
        started, _, _ = self.run_pool(ALGORITHM_PRIORITY, window=0)
        self.assertEqual(started, [54, 55, 56])

    def test_stop_during_window_discards_waiting_tasks_cleanly(self):
        pool = FakePool(self.METAS, gate=False, pool_size=1, dispatch_window_seconds=30)
        pool.start()
        pool.enqueue(54)
        pool.enqueue(55)
        began = time.monotonic()
        pool.stop(timeout=1)
        self.assertLess(time.monotonic() - began, 3)         # did not wait out 30 s
        self.assertEqual(pool.started, [])
        self.assertEqual(pool.get_task_status(54)["state"], "discarded")


class TestConfigAndAdminPage(unittest.TestCase):
    def setUp(self):
        worker_module.shutdown_worker()
        self.client = make_app().test_client()
        patcher = mock.patch("modules.admin.log_action")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = None

    def tearDown(self):
        worker_module._pool = None
        if self.pool is not None:
            self.pool.stop(timeout=2)

    def test_config_default_is_disabled(self):
        import config
        self.assertEqual(normalize_dispatch_window(config.Config.WORKER_DISPATCH_WINDOW_SECONDS), 0.0)

    def test_page_mentions_window_only_when_enabled(self):
        login(self.client)
        self.pool = FakePool({}, dispatch_window_seconds=0)
        self.pool.start()
        worker_module._pool = self.pool
        self.assertNotIn(b"Demo dispatch window", self.client.get("/admin/scheduler").data)
        self.pool.stop(timeout=2)

        self.pool = FakePool({}, dispatch_window_seconds=5)
        self.pool.start()
        worker_module._pool = self.pool
        html = self.client.get("/admin/scheduler").get_data(as_text=True)
        self.assertIn("Demo dispatch window", html)
        self.assertIn("5.0 s", html)


if __name__ == "__main__":
    unittest.main()
