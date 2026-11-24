"""
Regression tests for the "Priority looks like FCFS" report.

Screenshot scenario (today = 2026-10-27):
    #54  Machine Learning Intern   deadline 2026-10-28   applied first
    #55  Cloud Support Intern      deadline 2026-11-04   applied second
    #56  Database Developer        deadline 2026-10-29   applied third
Correct Priority order when the three are WAITING TOGETHER: #54, #56, #55.

Finding: the selection logic is correct. Scheduling here is non-preemptive
and work-conserving: an idle worker starts a task the moment it arrives, so
there is nothing to compare it with. Priority can only reorder tasks that are
waiting at the same time (a backlog). The last test pins that behaviour down
so it is explicit rather than a surprise.
"""

import unittest
from datetime import date, datetime, timedelta

from modules.scheduler import (
    ALGORITHM_FCFS, ALGORITHM_PRIORITY, SchedulerQueue, make_task,
)
from tests.test_worker_scheduling import BLOCKER, FakePool, meta, wait_until

FIXED_TODAY = date(2026, 10, 27)
T0 = datetime(2026, 10, 27, 15, 50, 0)


def screenshot_tasks():
    def task(app_id, minutes, deadline, title):
        return make_task(app_id, applied_at=T0 + timedelta(minutes=minutes),
                         deadline=deadline, job_title=title)
    return [
        task(54, 0, date(2026, 10, 28), "Machine Learning Intern"),
        task(55, 1, date(2026, 11, 4), "Cloud Support Intern"),
        task(56, 2, date(2026, 10, 29), "Database Developer"),
    ]


def drain(queue):
    out = []
    while queue.pending_count():
        out.append(queue.pop_next(timeout=1).task.application_id)
    return out


class ScreenshotOrderingAtSchedulerLevel(unittest.TestCase):
    def make_queue(self, algorithm):
        return SchedulerQueue(capacity=50, algorithm=algorithm,
                              today_func=lambda: FIXED_TODAY)

    def test_priority_picks_earliest_upcoming_deadline_first(self):
        q = self.make_queue(ALGORITHM_PRIORITY)
        for t in screenshot_tasks():
            q.add(t)
        self.assertEqual(drain(q), [54, 56, 55])

    def test_fcfs_on_the_same_tasks_is_arrival_order(self):
        q = self.make_queue(ALGORITHM_FCFS)
        for t in reversed(screenshot_tasks()):
            q.add(t)
        self.assertEqual(drain(q), [54, 55, 56])

    def test_priority_then_no_deadline_then_expired_with_tiebreakers(self):
        tasks = screenshot_tasks() + [
            make_task(57, applied_at=T0 + timedelta(minutes=3), deadline=None),
            make_task(58, applied_at=T0 + timedelta(minutes=4), deadline=date(2026, 10, 20)),
            make_task(59, applied_at=T0 + timedelta(minutes=2), deadline=None),   # earlier than 57
            make_task(60, applied_at=T0 + timedelta(minutes=5), deadline=date(2026, 10, 27)),  # today
            make_task(61, applied_at=T0 + timedelta(minutes=2), deadline=date(2026, 10, 19)),  # expired, earlier than 58
        ]
        q = self.make_queue(ALGORITHM_PRIORITY)
        for t in reversed(tasks):          # insertion order must not matter
            q.add(t)
        self.assertEqual(drain(q), [60, 54, 56, 55, 59, 57, 61, 58])

    def test_snapshot_positions_show_the_same_order(self):
        q = self.make_queue(ALGORITHM_PRIORITY)
        for t in screenshot_tasks():
            q.add(t)
        rows = sorted(q.snapshot()["tasks"], key=lambda r: r["position"])
        self.assertEqual([(r["position"], r["application_id"]) for r in rows],
                         [(1, 54), (2, 56), (3, 55)])


class ScreenshotOrderingInRealWorker(unittest.TestCase):
    """Same data through the real worker pool, deadlines relative to today."""

    METAS = {54: meta(0, 1), 55: meta(1, 8), 56: meta(2, 2)}   # +1d, +8d, +2d

    def test_backlog_behind_four_busy_workers_dispatches_54_56_55(self):
        pool = FakePool({**self.METAS, **{900 + i: meta(-100 - i) for i in range(4)}},
                        pool_size=4, scheduling_algorithm=ALGORITHM_PRIORITY)
        pool.start()
        try:
            for i in range(4):                     # occupy all four workers
                pool.enqueue(900 + i)
            self.assertTrue(wait_until(lambda: len(pool.started) == 4))
            for app_id in (54, 55, 56):            # arrive in application order
                pool.enqueue(app_id)
            shown = [r["application_id"] for r in
                     sorted(pool.get_scheduler_snapshot()["tasks"], key=lambda r: r["position"])]
            self.assertEqual(shown, [54, 56, 55])
            pool.gate.set()
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 7))
            self.assertEqual([i for i in pool.started if i < 900], [54, 56, 55])
            seqs = [pool.get_task_status(i)["dispatch_seq"] for i in (54, 56, 55)]
            self.assertEqual(seqs, sorted(seqs))
        finally:
            pool.stop(timeout=2)

    def test_same_backlog_under_fcfs_is_54_55_56(self):
        pool = FakePool({**self.METAS, BLOCKER: meta(-100)}, pool_size=1,
                        scheduling_algorithm=ALGORITHM_FCFS)
        pool.start()
        try:
            pool.enqueue(BLOCKER)
            self.assertTrue(wait_until(lambda: BLOCKER in pool.started))
            for app_id in (56, 54, 55):
                pool.enqueue(app_id)
            pool.gate.set()
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 4))
            self.assertEqual([i for i in pool.started if i != BLOCKER], [54, 55, 56])
        finally:
            pool.stop(timeout=2)

    def test_idle_workers_take_tasks_as_they_arrive_so_priority_cannot_reorder(self):
        """Documents the behaviour behind the report: each task found an idle
        worker, so it started at once, in arrival order, on a different worker."""
        pool = FakePool(self.METAS, gate=False, pool_size=4,
                        scheduling_algorithm=ALGORITHM_PRIORITY)
        pool.start()
        try:
            for app_id in (54, 55, 56):
                pool.enqueue(app_id)
                self.assertTrue(wait_until(lambda a=app_id: a in pool.started))
            self.assertTrue(wait_until(lambda: pool.get_stats()["completed"] == 3))
            self.assertEqual(pool.started, [54, 55, 56])
            workers = {pool.get_task_status(i)["worker"] for i in (54, 55, 56)}
            self.assertEqual(len(workers), 3)
            # nothing was waiting beside it, so nothing was passed over
            self.assertTrue(all(pool.get_task_status(i)["passed_over"] == 0 for i in (54, 55, 56)))
        finally:
            pool.stop(timeout=2)


if __name__ == "__main__":
    unittest.main()
