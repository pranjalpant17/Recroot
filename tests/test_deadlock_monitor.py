"""
Tests for the Deadlock Monitoring & Recovery dashboard.

Covers: real-event recording, rollback accounting, bounded retry, worker
recovery (including the recovery sweep), the pure view-model, and the admin
page (real data vs the separate simulation).

Run from the project root:

    python -m unittest tests.test_deadlock_monitor -v

No MySQL server, no network and no live `recroot` database are used. The
database is replaced by fakes, and the admin page's connection opener is
replaced in every test so `app.py` (which starts the real worker at import)
is never imported.
"""

import os
import re
import sys
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Importing the sibling module installs the stand-ins for anything not
# importable here (pymysql, missing project modules) before we import worker.
from tests.test_deadlock_handling import (  # noqa: E402
    FakeAdminDB, FakeConn, OperationalError, _make_app, deadlock, lock_timeout, make_pool,
)
from modules import db_retry, deadlock_demo, deadlock_monitor, worker  # noqa: E402
from modules.db_retry import MAX_EVENT_LOG, LockEventCounters, lock_events  # noqa: E402


def errors_of(snapshot):
    return [e for e in snapshot["events"] if e["type"] == "error"]


# ===========================================================================
# 1. Event recording (db_retry)
# ===========================================================================

class EventRecordingTests(unittest.TestCase):

    def test_error_event_carries_the_real_details(self):
        c = LockEventCounters()
        c.record_error(deadlock(), where="application #12", attempt=2, total_attempts=3,
                       retry_in=0.25, rolled_back=True)
        (event,) = c.snapshot()["events"]
        self.assertEqual((event["type"], event["kind"], event["code"]), ("error", "deadlock", 1213))
        self.assertEqual((event["where"], event["attempt"], event["total_attempts"]),
                         ("application #12", 2, 3))
        self.assertEqual(event["thread"], threading.current_thread().name)
        self.assertIn("rolled back", event["action"])
        self.assertIn("retry in 0.25 s", event["action"])
        self.assertIn("Deadlock found", event["message"])         # MySQL's own text
        self.assertTrue(event["at_text"])

    def test_events_are_newest_first_with_increasing_sequence(self):
        c = LockEventCounters()
        for n in range(3):
            c.record_error(deadlock(), where=f"application #{n}")
        events = c.snapshot()["events"]
        self.assertEqual([e["where"] for e in events],
                         ["application #2", "application #1", "application #0"])
        self.assertEqual([e["seq"] for e in events], [3, 2, 1])

    def test_final_attempt_is_described_as_no_retries_left(self):
        c = LockEventCounters()
        c.record_error(lock_timeout(), attempt=3, total_attempts=3, retry_in=None, rolled_back=True)
        event = c.snapshot()["events"][0]
        self.assertEqual(event["kind"], "lock_timeout")
        self.assertIn("no retries left", event["action"])

    def test_rollback_accounting(self):
        c = LockEventCounters()
        c.record_error(deadlock(), retry_in=0.1, rolled_back=True)
        c.record_error(deadlock(), retry_in=0.1, rolled_back=True)
        c.record_error(deadlock(), retry_in=0.1, rolled_back=False)
        c.record_error(deadlock(), retry_in=0.1, rolled_back=None)
        snap = c.snapshot()
        self.assertEqual((snap["rollbacks"], snap["rollback_failures"], snap["deadlocks"]), (2, 1, 4))
        self.assertTrue(any("rollback FAILED" in e["action"] for e in snap["events"]))

    def test_non_lock_errors_create_no_event_and_change_nothing(self):
        c = LockEventCounters()
        self.assertIsNone(c.record_error(OperationalError(1064, "syntax"), rolled_back=True))
        self.assertIsNone(c.record_error(ValueError("x"), rolled_back=True))
        snap = c.snapshot()
        self.assertEqual((snap["events"], snap["rollbacks"], snap["total_mysql_events"]), ([], 0, 0))

    def test_recovered_is_its_own_event(self):
        c = LockEventCounters()
        c.record_recovered(where="application #5", attempts=3)
        snap = c.snapshot()
        self.assertEqual(snap["recovered"], 1)
        self.assertEqual((snap["events"][0]["type"], snap["events"][0]["attempt"]), ("recovered", 3))
        self.assertEqual(snap["total_mysql_events"], 0)          # a recovery is not a lock error

    def test_log_is_bounded_but_counters_keep_counting(self):
        c = LockEventCounters()
        for n in range(MAX_EVENT_LOG + 50):
            c.record_error(deadlock(), where=f"application #{n}")
        snap = c.snapshot()
        self.assertEqual(snap["deadlocks"], MAX_EVENT_LOG + 50)
        self.assertEqual(len(snap["events"]), MAX_EVENT_LOG)
        self.assertEqual(snap["events"][0]["where"], f"application #{MAX_EVENT_LOG + 49}")

    def test_snapshot_is_a_copy(self):
        c = LockEventCounters()
        c.record_error(deadlock(), where="application #1")
        snap = c.snapshot()
        snap["events"][0]["where"] = "tampered"
        snap["events"].clear()
        snap["deadlocks"] = 999
        again = c.snapshot()
        self.assertEqual((again["deadlocks"], again["events"][0]["where"]), (1, "application #1"))

    def test_reset_clears_everything_and_restarts_the_clock(self):
        c = LockEventCounters()
        c.record_error(deadlock(), rolled_back=True)
        before = c.snapshot()["since"]
        time.sleep(0.01)
        c.reset()
        snap = c.snapshot()
        self.assertEqual((snap["events"], snap["rollbacks"], snap["deadlocks"]), ([], 0, 0))
        self.assertGreater(snap["since"], before)

    def test_concurrent_recording_loses_nothing(self):
        c = LockEventCounters()

        def burst():
            for _ in range(100):
                c.record_error(deadlock(), rolled_back=True)

        threads = [threading.Thread(target=burst) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = c.snapshot()
        self.assertEqual((snap["deadlocks"], snap["rollbacks"], len(snap["events"])), (600, 600, 200))
        seqs = [e["seq"] for e in snap["events"]]
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        self.assertEqual(len(set(seqs)), len(seqs))


class RetryHelperEventTests(unittest.TestCase):

    def setUp(self):
        self.c = LockEventCounters()
        self.rollbacks = []

    def run_op(self, op, rollback=None, **kw):
        return db_retry.run_with_lock_retry(
            op, rollback or (lambda: self.rollbacks.append(1)), sleep=lambda s: None,
            counters=self.c, jitter=0.0, base_delay=0.1, max_delay=1.0, where="job X", **kw)

    def test_recovery_records_each_failure_each_rollback_and_the_success(self):
        calls = []

        def op():
            calls.append(1)
            if len(calls) < 3:
                raise deadlock()
            return "ok"

        self.assertEqual(self.run_op(op, max_retries=3), "ok")
        snap = self.c.snapshot()
        self.assertEqual((snap["deadlocks"], snap["rollbacks"], snap["retries"], snap["recovered"]),
                         (2, 2, 2, 1))
        chronological = list(reversed(snap["events"]))
        self.assertEqual([(e["type"], e["attempt"]) for e in chronological],
                         [("error", 1), ("error", 2), ("recovered", 3)])
        self.assertIn("retry in 0.10 s", chronological[0]["action"])
        self.assertIn("retry in 0.20 s", chronological[1]["action"])

    def test_exhaustion_rolls_back_the_last_failure_too_and_says_so(self):
        def op():
            raise deadlock()

        with self.assertRaises(OperationalError):
            self.run_op(op, max_retries=2)
        snap = self.c.snapshot()
        self.assertEqual((snap["deadlocks"], snap["rollbacks"], snap["exhausted"], snap["recovered"]),
                         (3, 3, 1, 0))
        self.assertIn("no retries left", snap["events"][0]["action"])
        self.assertEqual(snap["events"][0]["attempt"], 3)

    def test_a_failing_rollback_is_counted_not_hidden(self):
        calls = []

        def op():
            calls.append(1)
            if len(calls) == 1:
                raise deadlock()
            return "ok"

        def bad_rollback():
            raise OperationalError(2006, "gone")

        self.run_op(op, rollback=bad_rollback)
        snap = self.c.snapshot()
        self.assertEqual((snap["rollbacks"], snap["rollback_failures"]), (0, 1))
        self.assertIn("rollback FAILED", errors_of(snap)[0]["action"])


# ===========================================================================
# 2. Worker: retry, rollback, event recording, recovery
# ===========================================================================

class RollbackConn(FakeConn):
    """FakeConn whose rollback can be made to fail."""
    rollback_fails = False

    def rollback(self):
        if self.rollback_fails:
            raise OperationalError(2006, "MySQL server has gone away")
        super().rollback()


class WorkerEventTests(unittest.TestCase):

    def setUp(self):
        lock_events.reset()
        self.addCleanup(lock_events.reset)
        self.pool = make_pool()
        self.conns = []
        self.fail_rollback_on_first = False

        def open_conn():
            conn = RollbackConn()
            if self.fail_rollback_on_first and not self.conns:
                conn.rollback_fails = True
            self.conns.append(conn)
            return conn

        mock.patch.object(self.pool, "_open_connection", side_effect=open_conn).start()
        mock.patch.object(worker.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def script(self, *outcomes):
        """Each call to _process_once consumes one outcome: exception or result."""
        queue = list(outcomes)

        def once(db, app_id):
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        self.pool._process_once = once

    def test_retry_records_attempts_rollbacks_and_the_recovery(self):
        self.script(deadlock(), lock_timeout(), (worker.STATE_DONE, None))
        self.assertEqual(self.pool._process_with_retries(7), (worker.STATE_DONE, None))

        snap = lock_events.snapshot()
        self.assertEqual((snap["deadlocks"], snap["lock_timeouts"], snap["retries"],
                          snap["rollbacks"], snap["recovered"], snap["exhausted"]),
                         (1, 1, 2, 2, 1, 0))
        chronological = list(reversed(snap["events"]))
        self.assertEqual([(e["kind"], e["attempt"], e["total_attempts"]) for e in chronological],
                         [("deadlock", 1, 3), ("lock_timeout", 2, 3), ("recovered", 3, None)])
        self.assertTrue(all(e["where"] == "application #7" for e in chronological))
        self.assertTrue(all(e["thread"] == threading.current_thread().name for e in chronological))
        self.assertTrue(all("rolled back" in e["action"] for e in chronological[:2]))
        self.assertEqual(sum(c.rollbacks for c in self.conns), 2)   # the DB really saw them

    def test_failed_rollback_discards_the_connection_and_is_reported(self):
        self.fail_rollback_on_first = True
        self.script(deadlock(), (worker.STATE_DONE, None))
        self.assertEqual(self.pool._process_with_retries(7)[0], worker.STATE_DONE)
        snap = lock_events.snapshot()
        self.assertEqual((snap["rollbacks"], snap["rollback_failures"], snap["recovered"]), (0, 1, 1))
        self.assertIn("rollback FAILED", errors_of(snap)[0]["action"])
        self.assertEqual(len(self.conns), 2)                          # fresh connection for the retry

    def test_exhausted_retries_are_recorded_and_the_task_is_failed_and_audited(self):
        self.script(deadlock(), deadlock(), deadlock())
        state, note = self.pool._process_with_retries(7)
        self.assertEqual(state, worker.STATE_FAILED)
        snap = lock_events.snapshot()
        self.assertEqual((snap["deadlocks"], snap["rollbacks"], snap["retries"],
                          snap["exhausted"], snap["recovered"]), (3, 3, 2, 1, 0))
        self.assertIn("no retries left", snap["events"][0]["action"])
        statements = [s[0] for c in self.conns for s in c.statements]
        self.assertTrue(any("INSERT INTO audit_log" in s for s in statements))   # audit row still written

    def test_ordinary_errors_record_nothing(self):
        self.script(OperationalError(1064, "syntax"))
        self.assertEqual(self.pool._process_with_retries(7)[0], worker.STATE_FAILED)
        snap = lock_events.snapshot()
        self.assertEqual((snap["events"], snap["rollbacks"], snap["retries"]), ([], 0, 0))

    def test_safe_rollback_reports_its_outcome(self):
        pool = make_pool()
        self.assertIsNone(pool._safe_rollback())                     # no connection yet
        good = RollbackConn()
        pool._local.conn = good
        self.assertIs(pool._safe_rollback(), True)
        self.assertEqual(good.rollbacks, 1)
        bad = RollbackConn()
        bad.rollback_fails = True
        pool._local.conn = bad
        self.assertIs(pool._safe_rollback(), False)
        self.assertIsNone(getattr(pool._local, "conn", None))        # discarded

    def test_stats_expose_retry_config_and_no_recovery_yet(self):
        stats = make_pool(max_retries=4, retry_backoff=0.75, lock_wait_timeout=12).get_stats()
        self.assertEqual((stats["max_retries"], stats["retry_backoff_seconds"],
                          stats["lock_wait_timeout_seconds"]), (4, 0.75, 12))
        self.assertIsNone(stats["last_recovery"])
        # the fields the existing Worker Monitor depends on are still there
        for key in ("running", "pool_size", "threads_alive", "active_workers", "queue_depth",
                    "queue_capacity", "in_flight", "retries", "scheduling_algorithm"):
            self.assertIn(key, stats)


class RecoverySweepTests(unittest.TestCase):

    class SweepConn(FakeConn):
        rows = []

        def cursor(self):
            cursor = super().cursor()
            cursor.fetchall = lambda: [dict(r) for r in self.rows]
            return cursor

    def setUp(self):
        lock_events.reset()
        self.addCleanup(lock_events.reset)
        self.addCleanup(mock.patch.stopall)

    def test_sweep_requeues_queued_processing_and_failed_and_remembers_it(self):
        pool = make_pool()
        pool._accepting = True                                       # producer side only
        when = datetime(2026, 1, 1, 9, 0)
        conn = self.SweepConn()
        conn.rows = [{"id": 2, "applied_at": when, "deadline": None, "job_title": "J"},
                     {"id": 3, "applied_at": when, "deadline": None, "job_title": "J"}]
        mock.patch.object(pool, "_open_connection", return_value=conn).start()

        summary = pool.recover_unprocessed()
        self.assertEqual((summary["found"], summary["enqueued"]), (2, 2))
        select = conn.statements[0]
        self.assertEqual(select[1], ("queued", "processing", "failed"))   # failed ones included
        self.assertTrue(select[0].startswith("SELECT"))
        self.assertEqual([s for s in conn.statements if not s[0].startswith("SELECT")], [])

        last = pool.get_stats()["last_recovery"]
        self.assertEqual((last["found"], last["enqueued"], last["include_failed"]), (2, 2, True))
        self.assertGreater(last["at"], 0)

    def test_task_that_exhausted_its_retries_is_recovered_by_the_next_sweep(self):
        """Full story with real worker threads: a task gives up after repeated
        deadlocks (marked failed + audited); a later recovery sweep re-queues
        it and, now that the conflict is gone, it completes."""
        pool = make_pool(pool_size=2, max_retries=2, retry_backoff=0.001)
        attempts, lock = {}, threading.Lock()
        phase = {"conflict": True}

        def once(db, app_id):
            with lock:
                attempts[app_id] = attempts.get(app_id, 0) + 1
                n = attempts[app_id]
            if app_id == 1 and n == 1:
                raise deadlock()                                      # recovers on retry
            if app_id == 2 and phase["conflict"]:
                raise deadlock()                                      # never recovers (yet)
            return worker.STATE_DONE, None

        pool._process_once = once
        sweep_conn = self.SweepConn()
        written = []

        def open_conn():
            conn = FakeConn()
            written.append(conn)
            return conn

        meta = {"applied_at": datetime(2026, 1, 1), "deadline": None, "job_title": "J"}
        with mock.patch.object(pool, "_open_connection", side_effect=open_conn):
            pool.start()
            try:
                self.assertTrue(pool.enqueue(1, meta))
                self.assertTrue(pool.enqueue(2, meta))
                self._wait(lambda: pool.get_stats()["completed"] + pool.get_stats()["failed"] == 2)
                stats = pool.get_stats()
                self.assertEqual((stats["completed"], stats["failed"]), (1, 1))

                snap = lock_events.snapshot()
                self.assertEqual((snap["deadlocks"], snap["recovered"], snap["exhausted"],
                                  snap["rollbacks"]), (4, 1, 1, 4))
                by_app = {}
                for e in snap["events"]:
                    by_app.setdefault(e["where"], []).append(e["type"])
                self.assertEqual(sorted(by_app["application #1"]), ["error", "recovered"])
                self.assertEqual(by_app["application #2"], ["error"] * 3)
                self.assertTrue(any("INSERT INTO audit_log" in s[0]
                                    for c in written for s in c.statements))

                # The conflict is gone; the sweep finds the failed row and re-queues it.
                phase["conflict"] = False
                sweep_conn.rows = [{"id": 2, "applied_at": datetime(2026, 1, 1),
                                    "deadline": None, "job_title": "J"}]
                with mock.patch.object(pool, "_temporary_connection") as temp:
                    temp.return_value.__enter__.return_value = sweep_conn
                    temp.return_value.__exit__.return_value = False
                    summary = pool.recover_unprocessed(include_failed=True)
                self.assertEqual(summary["enqueued"], 1)
                self._wait(lambda: pool.get_stats()["completed"] == 2)
            finally:
                pool.stop(timeout=5)

        self.assertEqual(pool.get_stats()["completed"], 2)
        self.assertEqual(attempts[2], 4)                              # 3 failed tries, then success
        self.assertEqual(lock_events.snapshot()["exhausted"], 1)      # history is not rewritten

    @staticmethod
    def _wait(condition, limit=20):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.02)
        raise AssertionError("condition not reached in time")


# ===========================================================================
# 3. View-model
# ===========================================================================

RUNNING_STATS = {"running": True, "pool_size": 4, "threads_alive": 4, "active_workers": 1,
                 "queue_depth": 2, "queue_capacity": 100, "in_flight": 3,
                 "completed": 9, "failed": 1, "enqueued": 10, "max_retries": 2,
                 "retry_backoff_seconds": 0.5, "lock_wait_timeout_seconds": 10,
                 "scheduling_algorithm": "priority", "last_recovery": None, "uptime_seconds": 90}


def snapshot_with(**kw):
    c = LockEventCounters()
    for _ in range(kw.get("deadlocks", 0)):
        c.record_error(deadlock(), where="application #1", attempt=1, total_attempts=3,
                       retry_in=0.1, rolled_back=True)
    for _ in range(kw.get("recovered", 0)):
        c.record_recovered(where="application #1", attempts=2)
    for _ in range(kw.get("exhausted", 0)):
        c.record_exhausted()
    for _ in range(kw.get("python", 0)):
        c.record_python_lock_timeout("application #3")
    return c.snapshot()


class ViewModelTests(unittest.TestCase):

    def test_quiet_when_nothing_was_observed(self):
        view = deadlock_monitor.build_dashboard(snapshot_with(), RUNNING_STATS, True)
        self.assertEqual(view["health"]["state"], "quiet")
        self.assertIn("No MySQL deadlock or lock wait timeout", view["health"]["message"])
        self.assertEqual(view["events"], [])
        self.assertIsNone(view["recovery_percent"])

    def test_recovered_state_and_percentage(self):
        view = deadlock_monitor.build_dashboard(snapshot_with(deadlocks=2, recovered=2), RUNNING_STATS, True)
        self.assertEqual(view["health"]["state"], "recovered")
        self.assertEqual((view["tasks_concluded"], view["recovery_percent"]), (2, 100))

    def test_attention_when_a_task_gave_up(self):
        view = deadlock_monitor.build_dashboard(
            snapshot_with(deadlocks=4, recovered=1, exhausted=1), RUNNING_STATS, True)
        self.assertEqual(view["health"]["state"], "attention")
        self.assertIn("1 task(s)", view["health"]["message"])
        self.assertEqual(view["recovery_percent"], 50)

    def test_python_lock_timeouts_alone_count_as_observed(self):
        view = deadlock_monitor.build_dashboard(snapshot_with(python=1), RUNNING_STATS, True)
        self.assertEqual(view["health"]["state"], "recovered")

    def test_worker_section_comes_from_the_existing_monitor_view(self):
        worker_view = deadlock_monitor.build_dashboard(snapshot_with(), RUNNING_STATS, True)["worker"]
        self.assertEqual(worker_view["status"], "running")
        self.assertEqual((worker_view["pool_size"], worker_view["queue_depth"], worker_view["in_flight"]),
                         (4, 2, 3))
        self.assertEqual((worker_view["max_retries"], worker_view["lock_wait_timeout_seconds"]), (2, 10))
        self.assertEqual(worker_view["policy"], "Priority (deadline)")

    def test_no_worker_pool_is_reported_honestly(self):
        view = deadlock_monitor.build_dashboard(snapshot_with(), {"running": False}, False)
        self.assertEqual(view["worker"]["status"], "not_initialised")
        self.assertIn("not running here", view["health"]["message"])

    def test_module_failure_is_reported(self):
        view = deadlock_monitor.build_dashboard(snapshot_with(), {}, False, module_error="ImportError")
        self.assertEqual(view["worker"]["status"], "unavailable")

    def test_event_rows_are_formatted_from_real_events_only(self):
        snap = snapshot_with(deadlocks=1, recovered=1)
        rows = deadlock_monitor.build_dashboard(snap, RUNNING_STATS, True)["events"]
        self.assertEqual(len(rows), 2)
        recovered, error = rows
        self.assertEqual((error["kind_label"], error["attempt_text"], error["where"]),
                         ("Deadlock (MySQL 1213)", "1 of 3", "application #1"))
        self.assertEqual((recovered["kind_label"], recovered["attempt_text"]), ("Recovered", "attempt 2"))
        self.assertTrue(error["is_error"] and not recovered["is_error"])

    def test_event_table_is_capped_but_reports_how_many_are_kept(self):
        snap = snapshot_with(deadlocks=deadlock_monitor.EVENTS_SHOWN + 25)
        view = deadlock_monitor.build_dashboard(snap, RUNNING_STATS, True)
        self.assertEqual(len(view["events"]), deadlock_monitor.EVENTS_SHOWN)
        self.assertEqual(view["events_total_kept"], deadlock_monitor.EVENTS_SHOWN + 25)

    def test_counters_pass_through_unmodified(self):
        snap = snapshot_with(deadlocks=3, recovered=2, exhausted=1, python=2)
        view = deadlock_monitor.build_dashboard(snap, RUNNING_STATS, True)
        for key in ("deadlocks", "lock_timeouts", "retries", "rollbacks", "recovered",
                    "exhausted", "python_lock_timeouts"):
            self.assertEqual(view["real"][key], snap[key], key)

    def test_live_view_reads_the_global_counters_and_the_live_pool(self):
        lock_events.reset()
        self.addCleanup(lock_events.reset)
        lock_events.record_error(deadlock(), where="application #9", attempt=1, total_attempts=3,
                                 retry_in=0.1, rolled_back=True)
        pool = make_pool(pool_size=3)
        with mock.patch.object(worker, "_pool", pool):
            view = deadlock_monitor.get_dashboard_view()
        self.assertEqual(view["real"]["deadlocks"], 1)
        self.assertEqual(view["worker"]["pool_size"], 3)
        with mock.patch.object(worker, "_pool", None):
            self.assertEqual(deadlock_monitor.get_dashboard_view()["worker"]["status"], "not_initialised")

    def test_simulations_do_not_influence_the_view(self):
        lock_events.reset()
        self.addCleanup(lock_events.reset)
        before = deadlock_monitor.get_dashboard_view()
        deadlock_demo.simulate_deadlock(timeout=0.2)
        deadlock_demo.run_prevention_demo(timeout=0.3)
        after = deadlock_monitor.get_dashboard_view()
        self.assertEqual(before["real"], after["real"])
        self.assertEqual(after["health"]["state"], "quiet")


class PersistedViewTests(unittest.TestCase):

    def test_reads_counts_and_recent_failures_with_select_only(self):
        db = FakeAdminDB(
            counts=[{"processing_status": "queued", "total": 2}, {"processing_status": "failed", "total": 3}],
            failures=[
                {"id": 9, "timestamp": "2026-10-01 10:00:00",
                 "details": "Application #4: OperationalError: (1213, 'Deadlock found')"},
                {"id": 8, "timestamp": "2026-10-01 09:00:00",
                 "details": "Application #2: ValueError: bad pdf"},
            ])
        view = deadlock_monitor.get_persisted_view(lambda: db)
        self.assertTrue(view["available"])
        counts = view["processing_counts"]
        self.assertEqual((counts["queued"], counts["failed"], counts["done"], counts["total"]), (2, 3, 0, 5))
        self.assertEqual([f["lock_related"] for f in view["recent_failures"]], [True, False])
        self.assertTrue(db.statements and all(s.startswith("SELECT") for s in db.statements))
        self.assertEqual(db.commits, 0)

    def test_lock_timeout_text_is_also_recognised(self):
        db = FakeAdminDB(failures=[{"id": 1, "timestamp": "t",
                                    "details": "Application #4: OperationalError: (1205, 'Lock wait timeout')"}])
        self.assertTrue(deadlock_monitor.get_persisted_view(lambda: db)["recent_failures"][0]["lock_related"])

    def test_unavailable_database_never_raises(self):
        def boom():
            raise RuntimeError("no app context")

        view = deadlock_monitor.get_persisted_view(boom)
        self.assertEqual((view["available"], view["error"]), (False, "RuntimeError"))
        broken = deadlock_monitor.get_persisted_view(lambda: FakeAdminDB(fail_with=OperationalError(2013, "lost")))
        self.assertEqual((broken["available"], broken["error"]), (False, OperationalError.__name__))


# ===========================================================================
# 4. The admin page
# ===========================================================================

def stat_value(html, label):
    match = re.search(r'stat-value">\s*(\d+)\s*</div>\s*<div class="stat-label">\s*' + re.escape(label), html)
    assert match, f"card {label!r} not found"
    return int(match.group(1))


class DashboardPageTests(unittest.TestCase):

    def setUp(self):
        lock_events.reset()
        self.addCleanup(lock_events.reset)
        from modules import admin
        self.admin = admin
        self.db = FakeAdminDB()
        self.open_db = mock.Mock(return_value=self.db)
        mock.patch.object(admin, "_open_db", self.open_db).start()
        real_sim = deadlock_demo.simulate_deadlock
        mock.patch.object(admin, "simulate_deadlock", lambda: real_sim(0.3)).start()
        self.addCleanup(mock.patch.stopall)
        self.client = _make_app().test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=1, role="admin", name="T", email="t@x")

    def get(self):
        return self.client.get("/admin/deadlock").get_data(as_text=True)

    def test_sections_are_present_and_the_simulation_comes_last(self):
        html = self.get()
        positions = [html.index(marker) for marker in (
            'id="health"', 'id="real-events"', 'id="worker-status"', 'id="event-log"',
            'id="persisted"', 'id="simulation"')]
        self.assertEqual(positions, sorted(positions))
        for text in ("Real MySQL lock events", "Worker status", "Recent real events",
                     "Persisted in the database", "Optional: deadlock simulation",
                     "Simulate Deadlock", "Run Prevention Demo"):
            self.assertIn(text, html)

    def test_real_and_simulated_are_labelled_separately(self):
        html = self.get()
        real_part, sim_part = html.split('id="simulation"')
        self.assertIn("<strong>REAL</strong>", real_part)
        self.assertNotIn("SIMULATION ONLY", real_part)
        self.assertNotIn("Simulate Deadlock", real_part)
        self.assertIn("SIMULATION ONLY", sim_part)
        self.assertIn("SIMULATED", sim_part)
        self.assertNotIn("<strong>REAL</strong>", sim_part)

    def test_fresh_process_shows_zeroes_and_no_fabricated_events(self):
        html = self.get()
        for label in ("Deadlocks", "Lock wait timeouts", "Retry attempts", "Rollbacks",
                      "Successful recoveries", "Gave up"):
            self.assertEqual(stat_value(html, label), 0, label)
        self.assertIn("No MySQL deadlock or lock wait timeout", html)
        self.assertIn("No lock events recorded", html)

    def test_real_events_show_up_with_their_details(self):
        lock_events.record_error(deadlock(), where="application #42", attempt=1, total_attempts=3,
                                 retry_in=0.4, rolled_back=True)
        lock_events.record_error(lock_timeout(), where="application #43", attempt=3, total_attempts=3,
                                 retry_in=None, rolled_back=True)
        lock_events.record_retry()
        lock_events.record_exhausted()
        lock_events.record_recovered(where="application #42", attempts=2)
        html = self.get()
        self.assertEqual(stat_value(html, "Deadlocks"), 1)
        self.assertEqual(stat_value(html, "Lock wait timeouts"), 1)
        self.assertEqual(stat_value(html, "Rollbacks"), 2)
        self.assertEqual(stat_value(html, "Retry attempts"), 1)
        self.assertEqual(stat_value(html, "Successful recoveries"), 1)
        self.assertEqual(stat_value(html, "Gave up"), 1)
        for text in ("Deadlock (MySQL 1213)", "Lock wait timeout (MySQL 1205)", "application #42",
                     "application #43", "1 of 3", "3 of 3", "retry in 0.40 s", "no retries left",
                     "succeeded on attempt 2", "Deadlock found when trying to get lock"):
            self.assertIn(text, html)
        self.assertIn("Attention", html)                              # a task gave up
        self.assertNotIn("No lock events recorded", html)

    def test_worker_status_and_recovery_info_are_shown(self):
        pool = make_pool(pool_size=3, max_retries=2, retry_backoff=0.5, lock_wait_timeout=10)
        pool._accepting = True
        pool._last_recovery = {"found": 4, "enqueued": 3, "already_in_flight": 0,
                               "left_for_later": 1, "include_failed": True,
                               "at": time.time()}
        with mock.patch.object(worker, "_pool", pool):
            html = self.get()
        self.assertIn("Running", html)
        self.assertIn("found 4", html)
        self.assertIn("re-queued 3", html)
        self.assertIn("left for later 1", html)
        self.assertIn("10 s", html)                                   # MySQL lock wait limit
        with mock.patch.object(worker, "_pool", None):
            self.assertIn("Not initialised", self.get())

    def test_running_a_simulation_leaves_the_real_section_untouched(self):
        lock_events.record_error(deadlock(), where="application #7", attempt=1, total_attempts=3,
                                 retry_in=0.1, rolled_back=True)
        before = lock_events.snapshot()
        r = self.client.post("/admin/deadlock/simulate")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        real_part, sim_part = html.split('id="simulation"')
        self.assertIn('id="demo-result"', sim_part)
        self.assertNotIn('id="demo-result"', real_part)
        self.assertIn("Deadlock reproduced", sim_part)
        self.assertEqual(stat_value(html, "Deadlocks"), 1)            # still exactly the real one
        self.assertEqual(real_part.count("Deadlock (MySQL 1213)"), 1)
        self.assertEqual(lock_events.snapshot(), before)
        self.assertIn('id="simulation" open', html)                   # result section is expanded

    def test_no_auto_refresh_on_a_post_result_but_offered_on_the_plain_page(self):
        self.assertIn('id="auto-refresh"', self.get())
        posted = self.client.post("/admin/deadlock/prevention").get_data(as_text=True)
        self.assertNotIn('id="auto-refresh"', posted)                 # reload would re-run the demo
        self.assertNotIn("window.location.reload", posted)

    def test_persisted_section_and_lock_related_labels(self):
        self.db.counts = [{"processing_status": "done", "total": 7}, {"processing_status": "failed", "total": 2}]
        self.db.failures = [
            {"id": 2, "timestamp": "2026-10-01 10:00:00",
             "details": "Application #4: OperationalError: (1213, 'Deadlock found')"},
            {"id": 1, "timestamp": "2026-10-01 09:00:00", "details": "Application #2: ValueError: x"},
        ]
        html = self.get()
        part = html[html.index('id="persisted"'):html.index('id="simulation"')]
        self.assertIn("<td>7</td>", part)
        self.assertIn("Application #4", part)
        self.assertIn("MySQL lock error (1213/1205)", part)
        self.assertIn("other cause", part)

    def test_unavailable_database_degrades_gracefully(self):
        self.open_db.side_effect = RuntimeError("no app context")
        lock_events.record_error(deadlock(), where="application #1", retry_in=0.1, rolled_back=True)
        r = self.client.get("/admin/deadlock")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("database view is unavailable", html)
        self.assertIn("RuntimeError", html)
        self.assertEqual(stat_value(html, "Deadlocks"), 1)            # live part unaffected

    def test_page_only_reads_the_database(self):
        self.get()
        self.client.post("/admin/deadlock/simulate")
        self.client.post("/admin/deadlock/prevention")
        self.assertTrue(self.db.statements)
        for sql in self.db.statements:
            self.assertTrue(sql.startswith("SELECT"), sql)
        self.assertEqual(self.db.commits, 0)                          # no writes, audit log untouched

    def test_access_control_is_unchanged(self):
        anonymous = _make_app().test_client()
        for method, url in (("get", "/admin/deadlock"), ("post", "/admin/deadlock/simulate"),
                            ("post", "/admin/deadlock/prevention")):
            r = getattr(anonymous, method)(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"])
        for role in ("candidate", "recruiter"):
            with self.client.session_transaction() as s:
                s["role"] = role
            self.assertEqual(self.client.get("/admin/deadlock").status_code, 302, role)
        self.open_db.reset_mock()
        with self.client.session_transaction() as s:
            s["role"] = "candidate"
        self.client.get("/admin/deadlock")
        self.open_db.assert_not_called()                              # rejected before any DB access

    def test_nav_link_points_at_the_monitor(self):
        html = self.get()
        self.assertIn('href="/admin/deadlock"', html)
        self.assertIn("Deadlock Monitor", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)