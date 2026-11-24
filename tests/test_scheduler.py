"""
Tests for modules/scheduler.py (FCFS + deadline-based priority scheduling).

Standalone: no database, no PDF files, no Flask app. Run from the project
root with either:

    python -m unittest tests.test_scheduler -v
    python tests/test_scheduler.py

"today" is fixed to 26 Oct 2026 so every deadline-dependent result is
repeatable.
"""

import os
import queue
import random
import sys
import threading
import time
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import scheduler as sch  # noqa: E402
from modules.scheduler import (  # noqa: E402
    ADD_CLOSED,
    ADD_DUPLICATE,
    ADD_FULL,
    ADD_HELD,
    ADD_QUEUED,
    ALGORITHM_FCFS,
    ALGORITHM_PRIORITY,
    BUCKET_EXPIRED,
    BUCKET_NO_DEADLINE,
    BUCKET_UPCOMING,
    SchedulerQueue,
    SchedulerStateError,
    deadline_bucket,
    make_task,
    order_tasks,
    queue_positions,
)

TODAY = date(2026, 10, 26)
WAIT = 5.0   # generous timeout so a bug fails a test instead of hanging it


def T(app_id, applied="2026-10-20 09:00:00", deadline=None, title=None):
    """A task with a fixed arrival time (so tests never depend on the clock)."""
    return make_task(
        app_id,
        applied_at=datetime.fromisoformat(applied),
        deadline=deadline,
        job_title=title or f"Job {app_id}",
        enqueued_at=1.0,
    )


def ids(tasks):
    return [t.application_id for t in tasks]


def example_tasks():
    """
    The worked example from the design (today = 2026-10-26). Ids are in
    arrival order; applied_at increases with the id.
        7   expired        (deadline 10-20)
        9   upcoming       (deadline 10-28)
        12  upcoming       (deadline 10-28)
        15  upcoming       (deadline 10-27)
        20  no deadline
    """
    return [
        T(7,  "2026-10-20 09:00:00", "2026-10-20"),
        T(9,  "2026-10-21 09:00:00", "2026-10-28"),
        T(12, "2026-10-22 09:00:00", "2026-10-28"),
        T(15, "2026-10-23 09:00:00", "2026-10-27"),
        T(20, "2026-10-24 09:00:00", None),
    ]


def new_queue(**kwargs):
    kwargs.setdefault("today_func", lambda: TODAY)
    return SchedulerQueue(**kwargs)


def drain(q, count):
    """Pop `count` decisions; each pop must succeed within WAIT seconds."""
    return [q.pop_next(timeout=WAIT) for _ in range(count)]


# ------------------------------------------------------------
# Deadline buckets
# ------------------------------------------------------------

class BucketTests(unittest.TestCase):

    def test_future_deadline_is_upcoming(self):
        self.assertEqual(deadline_bucket(date(2026, 10, 27), TODAY), BUCKET_UPCOMING)

    def test_deadline_today_is_still_upcoming(self):
        # A DATE has no time of day, so the whole of the deadline day counts.
        self.assertEqual(deadline_bucket(TODAY, TODAY), BUCKET_UPCOMING)

    def test_past_deadline_is_expired(self):
        self.assertEqual(deadline_bucket(date(2026, 10, 25), TODAY), BUCKET_EXPIRED)

    def test_no_deadline_bucket(self):
        self.assertEqual(deadline_bucket(None, TODAY), BUCKET_NO_DEADLINE)

    def test_accepts_datetime_and_iso_string(self):
        self.assertEqual(deadline_bucket(datetime(2026, 10, 27, 15, 30), TODAY), BUCKET_UPCOMING)
        self.assertEqual(deadline_bucket("2026-10-25", TODAY), BUCKET_EXPIRED)
        self.assertEqual(deadline_bucket("", TODAY), BUCKET_NO_DEADLINE)

    def test_invalid_deadline_is_rejected_not_ignored(self):
        with self.assertRaises(ValueError):
            deadline_bucket("not-a-date", TODAY)
        with self.assertRaises(TypeError):
            deadline_bucket(12345, TODAY)


# ------------------------------------------------------------
# make_task
# ------------------------------------------------------------

class MakeTaskTests(unittest.TestCase):

    def test_normalises_values(self):
        task = make_task("42", applied_at="2026-10-20 09:00:00",
                         deadline=datetime(2026, 11, 1, 8, 0), job_title="X",
                         enqueued_at=5)
        self.assertEqual(task.application_id, 42)
        self.assertEqual(task.deadline, date(2026, 11, 1))
        self.assertEqual(task.applied_at, datetime(2026, 10, 20, 9, 0))
        self.assertFalse(task.metadata_missing)

    def test_missing_applied_at_falls_back_and_is_flagged(self):
        task = make_task(5, applied_at=None, enqueued_at=1_000_000.0)
        self.assertTrue(task.metadata_missing)
        self.assertEqual(task.applied_at, datetime.fromtimestamp(1_000_000.0))

    def test_missing_deadline_is_not_missing_metadata(self):
        task = make_task(5, applied_at=datetime(2026, 10, 20), deadline=None)
        self.assertFalse(task.metadata_missing)
        self.assertIsNone(task.deadline)

    def test_timezone_aware_applied_at_is_made_naive(self):
        from datetime import timezone
        aware = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
        task = make_task(1, applied_at=aware)
        self.assertIsNone(task.applied_at.tzinfo)


# ------------------------------------------------------------
# FCFS ordering
# ------------------------------------------------------------

class FcfsOrderTests(unittest.TestCase):

    def test_orders_by_arrival_time(self):
        tasks = [T(3, "2026-10-22 09:00:00"), T(1, "2026-10-20 09:00:00"),
                 T(2, "2026-10-21 09:00:00")]
        self.assertEqual(ids(order_tasks(tasks, ALGORITHM_FCFS, TODAY)), [1, 2, 3])

    def test_equal_arrival_time_breaks_tie_by_application_id(self):
        # applied_at has 1-second resolution, so equal timestamps really occur
        tasks = [T(52, "2026-10-20 09:00:00"), T(50, "2026-10-20 09:00:00"),
                 T(51, "2026-10-20 09:00:00")]
        self.assertEqual(ids(order_tasks(tasks, ALGORITHM_FCFS, TODAY)), [50, 51, 52])

    def test_arrival_time_wins_over_application_id(self):
        tasks = [T(10, "2026-10-21 09:00:00"), T(11, "2026-10-20 09:00:00")]
        self.assertEqual(ids(order_tasks(tasks, ALGORITHM_FCFS, TODAY)), [11, 10])

    def test_fcfs_ignores_deadlines_completely(self):
        tasks = [T(1, "2026-10-20 09:00:00", "2026-12-31"),
                 T(2, "2026-10-21 09:00:00", "2026-10-26"),
                 T(3, "2026-10-22 09:00:00", "2026-10-01")]
        self.assertEqual(ids(order_tasks(tasks, ALGORITHM_FCFS, TODAY)), [1, 2, 3])


# ------------------------------------------------------------
# Priority (deadline) ordering
# ------------------------------------------------------------

class PriorityOrderTests(unittest.TestCase):

    def prio(self, tasks, today=TODAY):
        return ids(order_tasks(tasks, ALGORITHM_PRIORITY, today))

    def test_earliest_upcoming_deadline_first(self):
        tasks = [T(1, deadline="2026-11-10"), T(2, deadline="2026-10-30"),
                 T(3, deadline="2026-11-02")]
        self.assertEqual(self.prio(tasks), [2, 3, 1])

    def test_deadline_beats_arrival_time(self):
        late_but_urgent = T(2, "2026-10-25 09:00:00", "2026-10-27")
        early_but_relaxed = T(1, "2026-10-20 09:00:00", "2026-11-30")
        self.assertEqual(self.prio([early_but_relaxed, late_but_urgent]), [2, 1])

    def test_deadline_today_is_most_urgent(self):
        tasks = [T(1, deadline="2026-10-27"), T(2, deadline="2026-10-26")]
        self.assertEqual(self.prio(tasks), [2, 1])

    def test_equal_deadlines_break_tie_by_applied_at(self):
        tasks = [T(40, "2026-10-22 10:00:00", "2026-10-30"),
                 T(41, "2026-10-22 09:00:00", "2026-10-30")]
        self.assertEqual(self.prio(tasks), [41, 40])

    def test_equal_deadline_and_applied_at_break_tie_by_id(self):
        tasks = [T(51, "2026-10-22 09:00:00", "2026-10-30"),
                 T(50, "2026-10-22 09:00:00", "2026-10-30")]
        self.assertEqual(self.prio(tasks), [50, 51])

    def test_no_deadline_comes_after_every_upcoming_deadline(self):
        tasks = [T(1, "2026-10-01 09:00:00", None),
                 T(2, "2026-10-25 09:00:00", "2027-12-31")]
        self.assertEqual(self.prio(tasks), [2, 1])

    def test_no_deadline_orders_by_applied_at_then_id(self):
        tasks = [T(3, "2026-10-22 09:00:00"), T(2, "2026-10-21 09:00:00"),
                 T(1, "2026-10-21 09:00:00")]
        self.assertEqual(self.prio(tasks), [1, 2, 3])

    def test_expired_comes_after_no_deadline(self):
        tasks = [T(1, "2026-10-01 09:00:00", "2026-10-20"),   # expired, arrived first
                 T(2, "2026-10-25 09:00:00", None)]
        self.assertEqual(self.prio(tasks), [2, 1])

    def test_expired_orders_by_applied_at_not_by_deadline(self):
        # Ordering by deadline would give [31, 30]
        tasks = [T(30, "2026-10-01 09:00:00", "2026-10-20"),
                 T(31, "2026-10-02 09:00:00", "2026-10-10")]
        self.assertEqual(self.prio(tasks), [30, 31])

    def test_expired_are_not_dropped(self):
        tasks = [T(1, deadline="2026-01-01"), T(2, deadline="2026-02-01")]
        self.assertEqual(sorted(self.prio(tasks)), [1, 2])

    def test_worked_example_priority_vs_fcfs(self):
        tasks = example_tasks()
        self.assertEqual(ids(order_tasks(tasks, ALGORITHM_FCFS, TODAY)),
                         [7, 9, 12, 15, 20])
        self.assertEqual(self.prio(tasks), [15, 9, 12, 20, 7])

    def test_buckets_are_re_evaluated_for_the_date_given(self):
        # Two days later the 10-27 and 10-28 deadlines have all expired.
        tasks = example_tasks()
        later = date(2026, 10, 29)
        self.assertEqual(self.prio(tasks, today=later), [20, 7, 9, 12, 15])

    def test_result_does_not_depend_on_input_order(self):
        tasks = example_tasks()
        expected = self.prio(tasks)
        rng = random.Random(1234)
        for _ in range(25):
            shuffled = tasks[:]
            rng.shuffle(shuffled)
            self.assertEqual(self.prio(shuffled), expected)

    def test_unknown_algorithm_is_rejected(self):
        with self.assertRaises(ValueError):
            order_tasks(example_tasks(), "round_robin", TODAY)

    def test_queue_positions_start_at_one(self):
        result = queue_positions(example_tasks(), ALGORITHM_PRIORITY, TODAY)
        self.assertEqual([(p, t.application_id) for p, t in result],
                         [(1, 15), (2, 9), (3, 12), (4, 20), (5, 7)])


# ------------------------------------------------------------
# SchedulerQueue: automatic mode
# ------------------------------------------------------------

class AutomaticQueueTests(unittest.TestCase):

    def fill(self, q, tasks=None):
        for t in (tasks or example_tasks()):
            self.assertEqual(q.add(t), ADD_QUEUED)

    def test_fcfs_pop_order(self):
        q = new_queue(algorithm=ALGORITHM_FCFS)
        self.fill(q)
        self.assertEqual([d.task.application_id for d in drain(q, 5)],
                         [7, 9, 12, 15, 20])

    def test_priority_pop_order(self):
        q = new_queue(algorithm=ALGORITHM_PRIORITY)
        self.fill(q)
        self.assertEqual([d.task.application_id for d in drain(q, 5)],
                         [15, 9, 12, 20, 7])

    def test_decision_facts(self):
        q = new_queue(algorithm=ALGORITHM_PRIORITY)
        self.fill(q)
        first, second = drain(q, 2)
        self.assertEqual(first.task.application_id, 15)
        self.assertEqual(first.dispatch_seq, 1)
        self.assertEqual(second.dispatch_seq, 2)
        self.assertEqual(first.queue_size, 5)
        self.assertEqual(second.queue_size, 4)
        self.assertEqual(first.bucket, BUCKET_UPCOMING)
        self.assertEqual(first.algorithm, ALGORITHM_PRIORITY)
        self.assertEqual(first.mode, "automatic")
        self.assertIsNone(first.run_id)
        # 15 arrived 4th; 7, 9 and 12 arrived earlier and were jumped over
        self.assertEqual(first.passed_over, 3)

    def test_fcfs_never_passes_anyone_over(self):
        q = new_queue(algorithm=ALGORITHM_FCFS)
        self.fill(q)
        self.assertEqual([d.passed_over for d in drain(q, 5)], [0, 0, 0, 0, 0])

    def test_dispatch_sequence_numbers_are_consecutive(self):
        q = new_queue()
        self.fill(q)
        self.assertEqual([d.dispatch_seq for d in drain(q, 5)], [1, 2, 3, 4, 5])

    def test_algorithm_switch_applies_to_next_selection(self):
        q = new_queue(algorithm=ALGORITHM_FCFS)
        self.fill(q)
        self.assertEqual(q.pop_next(timeout=WAIT).task.application_id, 7)   # FCFS pick
        previous = q.set_algorithm(ALGORITHM_PRIORITY)
        self.assertEqual(previous, ALGORITHM_FCFS)
        self.assertEqual(q.pop_next(timeout=WAIT).task.application_id, 15)  # priority pick

    def test_priority_is_decided_at_dispatch_time_not_enqueue_time(self):
        clock = [TODAY]
        q = SchedulerQueue(algorithm=ALGORITHM_PRIORITY, today_func=lambda: clock[0])
        q.add(T(1, "2026-10-24 09:00:00", "2026-10-27"))   # upcoming when queued
        q.add(T(2, "2026-10-25 09:00:00", None))
        clock[0] = date(2026, 10, 28)                       # a day later: 1 has expired
        first = q.pop_next(timeout=WAIT)
        self.assertEqual(first.task.application_id, 2)
        self.assertEqual(q.pop_next(timeout=WAIT).bucket, BUCKET_EXPIRED)

    def test_snapshot_positions_match_the_real_pop_order(self):
        for algorithm in (ALGORITHM_FCFS, ALGORITHM_PRIORITY):
            with self.subTest(algorithm=algorithm):
                q = new_queue(algorithm=algorithm)
                self.fill(q)
                shown = [r["application_id"] for r in
                         sorted(q.snapshot()["tasks"], key=lambda r: r["position"])]
                popped = [d.task.application_id for d in drain(q, 5)]
                self.assertEqual(shown, popped)

    def test_snapshot_describes_each_task(self):
        q = new_queue(algorithm=ALGORITHM_PRIORITY)
        self.fill(q)
        snap = q.snapshot()
        self.assertEqual(snap["mode"], "automatic")
        self.assertEqual(snap["eligible_count"], 5)
        self.assertEqual(snap["held_count"], 0)
        by_id = {r["application_id"]: r for r in snap["tasks"]}
        self.assertEqual(by_id[15]["position"], 1)
        self.assertEqual(by_id[15]["bucket_label"], "upcoming")
        self.assertEqual(by_id[20]["bucket_label"], "no deadline")
        self.assertEqual(by_id[7]["bucket_label"], "expired")
        self.assertEqual(by_id[7]["position"], 5)
        self.assertEqual(by_id[15]["job_title"], "Job 15")

    def test_pop_times_out_when_nothing_is_waiting(self):
        q = new_queue()
        with self.assertRaises(queue.Empty):
            q.pop_next(timeout=0.05)

    def test_blocked_pop_wakes_up_when_a_task_arrives(self):
        q = new_queue()
        got = []
        worker = threading.Thread(target=lambda: got.append(q.pop_next(timeout=WAIT)))
        worker.start()
        time.sleep(0.05)
        q.add(T(1))
        worker.join(WAIT)
        self.assertFalse(worker.is_alive())
        self.assertEqual(got[0].task.application_id, 1)

    def test_capacity_is_enforced(self):
        q = new_queue(capacity=2)
        self.assertEqual(q.add(T(1)), ADD_QUEUED)
        self.assertEqual(q.add(T(2)), ADD_QUEUED)
        self.assertEqual(q.add(T(3)), ADD_FULL)
        q.pop_next(timeout=WAIT)
        self.assertEqual(q.add(T(3)), ADD_QUEUED)

    def test_same_application_cannot_wait_twice(self):
        q = new_queue()
        self.assertEqual(q.add(T(1)), ADD_QUEUED)
        self.assertEqual(q.add(T(1)), ADD_DUPLICATE)
        self.assertEqual(q.pending_count(), 1)

    def test_close_refuses_new_work_and_wakes_blocked_workers(self):
        q = new_queue()
        got = []
        worker = threading.Thread(target=lambda: got.append(q.pop_next()))
        worker.start()
        time.sleep(0.05)
        q.close()
        worker.join(WAIT)
        self.assertFalse(worker.is_alive())
        self.assertEqual(got, [None])
        self.assertEqual(q.add(T(1)), ADD_CLOSED)

    def test_discard_all_empties_the_queue(self):
        q = new_queue()
        for t in example_tasks():
            q.add(t)
        removed = q.discard_all()
        self.assertEqual(sorted(ids(removed)), [7, 9, 12, 15, 20])
        self.assertEqual(q.pending_count(), 0)

    def test_invalid_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            SchedulerQueue(capacity=0)
        with self.assertRaises(ValueError):
            SchedulerQueue(algorithm="lottery")
        with self.assertRaises(ValueError):
            SchedulerQueue(mode="whenever")
        with self.assertRaises(ValueError):
            new_queue().set_algorithm("lottery")
        with self.assertRaises(ValueError):
            new_queue().set_mode("whenever")


# ------------------------------------------------------------
# SchedulerQueue: manual mode
# ------------------------------------------------------------

class ManualModeTests(unittest.TestCase):

    def manual_queue(self, **kwargs):
        return new_queue(mode="manual", **kwargs)

    def hold_example(self, q):
        for t in example_tasks():
            self.assertEqual(q.add(t), ADD_HELD)

    def test_tasks_are_held_and_workers_do_not_take_them(self):
        q = self.manual_queue()
        self.hold_example(q)
        with self.assertRaises(queue.Empty):
            q.pop_next(timeout=0.1)
        snap = q.snapshot()
        self.assertEqual((snap["held_count"], snap["eligible_count"]), (5, 0))
        self.assertTrue(all(r["state"] == "held" for r in snap["tasks"]))

    def test_run_scheduler_releases_in_priority_order(self):
        q = self.manual_queue(algorithm=ALGORITHM_PRIORITY)
        self.hold_example(q)
        run = q.run_scheduler()
        self.assertEqual(run["run_id"], 1)
        self.assertEqual(run["algorithm"], ALGORITHM_PRIORITY)
        self.assertEqual(run["size"], 5)
        self.assertEqual(run["planned_order"], [15, 9, 12, 20, 7])
        decisions = drain(q, 5)
        self.assertEqual([d.task.application_id for d in decisions], [15, 9, 12, 20, 7])
        self.assertEqual({d.run_id for d in decisions}, {1})
        self.assertEqual({d.mode for d in decisions}, {"manual"})

    def test_run_scheduler_releases_in_fcfs_order(self):
        q = self.manual_queue(algorithm=ALGORITHM_FCFS)
        self.hold_example(q)
        run = q.run_scheduler()
        self.assertEqual(run["planned_order"], [7, 9, 12, 15, 20])
        self.assertEqual([d.task.application_id for d in drain(q, 5)],
                         [7, 9, 12, 15, 20])

    def test_planned_order_matches_actual_dispatch_order(self):
        for algorithm in (ALGORITHM_FCFS, ALGORITHM_PRIORITY):
            with self.subTest(algorithm=algorithm):
                q = self.manual_queue(algorithm=algorithm)
                self.hold_example(q)
                run = q.run_scheduler()
                popped = [d.task.application_id for d in drain(q, 5)]
                self.assertEqual(run["planned_order"], popped)

    def test_held_preview_matches_what_the_run_then_does(self):
        q = self.manual_queue(algorithm=ALGORITHM_PRIORITY)
        self.hold_example(q)
        preview = [r["application_id"] for r in
                   sorted(q.snapshot()["tasks"], key=lambda r: r["position"])]
        q.run_scheduler()
        self.assertEqual(preview, [d.task.application_id for d in drain(q, 5)])

    def test_run_keeps_the_algorithm_it_started_with(self):
        q = self.manual_queue(algorithm=ALGORITHM_PRIORITY)
        self.hold_example(q)
        q.run_scheduler()
        q.set_algorithm(ALGORITHM_FCFS)                 # changed mid-run
        popped = [d.task.application_id for d in drain(q, 5)]
        self.assertEqual(popped, [15, 9, 12, 20, 7])    # still the priority run
        self.assertEqual(q.get_algorithm(), ALGORITHM_FCFS)

    def test_tasks_arriving_during_a_run_stay_held(self):
        q = self.manual_queue()
        self.hold_example(q)
        q.run_scheduler()
        self.assertEqual(q.add(T(99, "2026-10-25 09:00:00", "2026-10-26")), ADD_HELD)
        popped = [d.task.application_id for d in drain(q, 5)]
        self.assertNotIn(99, popped)
        with self.assertRaises(queue.Empty):
            q.pop_next(timeout=0.05)
        self.assertEqual(q.snapshot()["held_count"], 1)

    def test_second_run_picks_up_the_new_batch(self):
        q = self.manual_queue(algorithm=ALGORITHM_PRIORITY)
        self.hold_example(q)
        q.run_scheduler()
        drain(q, 5)
        q.add(T(99, "2026-10-25 09:00:00", "2026-10-26"))
        run2 = q.run_scheduler()
        self.assertEqual(run2["run_id"], 2)
        self.assertEqual(run2["planned_order"], [99])

    def test_run_refused_in_automatic_mode(self):
        q = new_queue()
        with self.assertRaises(SchedulerStateError) as ctx:
            q.run_scheduler()
        self.assertIn("manual mode", str(ctx.exception))

    def test_run_refused_when_nothing_is_held(self):
        q = self.manual_queue()
        with self.assertRaises(SchedulerStateError) as ctx:
            q.run_scheduler()
        self.assertIn("No held", str(ctx.exception))

    def test_run_refused_while_previous_run_still_waiting(self):
        q = self.manual_queue()
        self.hold_example(q)
        q.run_scheduler()
        drain(q, 2)                                     # 3 still waiting
        q.add(T(99))
        with self.assertRaises(SchedulerStateError) as ctx:
            q.run_scheduler()
        self.assertIn("3 application", str(ctx.exception))

    def test_switching_to_automatic_releases_held_tasks(self):
        q = self.manual_queue(algorithm=ALGORITHM_PRIORITY)
        self.hold_example(q)
        self.assertEqual(q.set_mode("automatic"), 5)
        self.assertEqual([d.task.application_id for d in drain(q, 5)],
                         [15, 9, 12, 20, 7])

    def test_switching_to_manual_holds_unclaimed_tasks(self):
        q = new_queue(algorithm=ALGORITHM_FCFS)
        for t in example_tasks():
            q.add(t)
        q.pop_next(timeout=WAIT)                        # one already taken by a worker
        self.assertEqual(q.set_mode("manual"), 4)
        with self.assertRaises(queue.Empty):
            q.pop_next(timeout=0.05)
        self.assertEqual(q.snapshot()["held_count"], 4)

    def test_setting_the_same_mode_changes_nothing(self):
        q = new_queue()
        self.assertEqual(q.set_mode("automatic"), 0)

    def test_capacity_counts_held_tasks_too(self):
        q = self.manual_queue(capacity=2)
        self.assertEqual(q.add(T(1)), ADD_HELD)
        self.assertEqual(q.add(T(2)), ADD_HELD)
        self.assertEqual(q.add(T(3)), ADD_FULL)

    def test_automatic_mode_unaffected_by_manual_machinery(self):
        # Regression guard: a fresh queue is automatic and starts work at once.
        q = new_queue()
        q.add(T(1))
        self.assertEqual(q.pop_next(timeout=WAIT).task.application_id, 1)


# ------------------------------------------------------------
# Concurrency: many threads, no task lost or taken twice
# ------------------------------------------------------------

class ConcurrencyTests(unittest.TestCase):

    def run_consumers(self, q, expected, consumers=8):
        """Start consumer threads, wait until `expected` tasks have been
        taken, then close the queue. Returns the list of decisions."""
        taken, lock = [], threading.Lock()

        def consume():
            while True:
                decision = q.pop_next()
                if decision is None:
                    return
                with lock:
                    taken.append(decision)

        threads = [threading.Thread(target=consume, daemon=True) for _ in range(consumers)]
        for t in threads:
            t.start()
        end = time.monotonic() + 10
        while time.monotonic() < end:
            with lock:
                if len(taken) >= expected:
                    break
            time.sleep(0.01)
        time.sleep(0.05)          # a short grace period to expose any extra pops
        q.close()
        for t in threads:
            t.join(WAIT)
            self.assertFalse(t.is_alive())
        return taken

    def make_many(self, count):
        rng = random.Random(7)
        tasks = []
        for i in range(1, count + 1):
            deadline = rng.choice([None, "2026-10-20", "2026-10-27", "2026-11-05", "2026-12-01"])
            tasks.append(T(i, f"2026-10-{rng.randint(1, 25):02d} 09:00:00", deadline))
        return tasks

    def test_each_task_is_taken_exactly_once_by_competing_workers(self):
        count = 300
        q = new_queue(capacity=500, algorithm=ALGORITHM_PRIORITY)
        threads = []
        tasks = self.make_many(count)
        for chunk in (tasks[0:100], tasks[100:200], tasks[200:300]):   # 3 producers
            threads.append(threading.Thread(
                target=lambda c=chunk: [q.add(t) for t in c], daemon=True))
        consumers_done = []
        runner = threading.Thread(
            target=lambda: consumers_done.append(self.run_consumers(q, count)), daemon=True)
        runner.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
        runner.join(20)
        taken = consumers_done[0]
        taken_ids = [d.task.application_id for d in taken]
        self.assertEqual(len(taken_ids), count)
        self.assertEqual(sorted(taken_ids), list(range(1, count + 1)))   # none lost, none twice
        seqs = sorted(d.dispatch_seq for d in taken)
        self.assertEqual(seqs, list(range(1, count + 1)))                # unique, consecutive

    def test_prefilled_queue_with_competing_workers_loses_nothing(self):
        count = 200
        q = new_queue(capacity=500, algorithm=ALGORITHM_FCFS)
        for t in self.make_many(count):
            self.assertEqual(q.add(t), ADD_QUEUED)
        taken = self.run_consumers(q, count)
        self.assertEqual(sorted(d.task.application_id for d in taken),
                         list(range(1, count + 1)))

    def test_selection_order_is_exact_when_one_worker_drains_the_queue(self):
        tasks = self.make_many(150)
        for algorithm in (ALGORITHM_FCFS, ALGORITHM_PRIORITY):
            with self.subTest(algorithm=algorithm):
                q = new_queue(capacity=500, algorithm=algorithm)
                for t in tasks:
                    q.add(t)
                popped = [q.pop_next(timeout=WAIT).task for _ in tasks]
                self.assertEqual(ids(popped), ids(order_tasks(tasks, algorithm, TODAY)))

    def test_manual_run_hands_each_held_task_to_exactly_one_worker(self):
        count = 120
        q = new_queue(capacity=500, algorithm=ALGORITHM_PRIORITY, mode="manual")
        for t in self.make_many(count):
            self.assertEqual(q.add(t), ADD_HELD)
        done = []
        runner = threading.Thread(
            target=lambda: done.append(self.run_consumers(q, count, consumers=6)), daemon=True)
        runner.start()
        time.sleep(0.15)
        self.assertEqual(q.snapshot()["dispatched_total"], 0)   # nothing moves before the click
        q.run_scheduler()
        runner.join(20)
        taken_ids = [d.task.application_id for d in done[0]]
        self.assertEqual(sorted(taken_ids), list(range(1, count + 1)))

    def test_duplicate_adds_from_many_threads_are_accepted_once(self):
        q = new_queue()
        results, lock = [], threading.Lock()
        barrier = threading.Barrier(12)

        def attempt():
            barrier.wait()
            outcome = q.add(T(1))
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
        self.assertEqual(results.count(ADD_QUEUED), 1)
        self.assertEqual(results.count(ADD_DUPLICATE), 11)
        self.assertEqual(q.pending_count(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)