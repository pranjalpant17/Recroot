"""
Tests for deadlock handling (lock ordering, MySQL 1213/1205 retry, demo page).

Run from the project root (works with unittest and pytest):

    python -m unittest tests.test_deadlock_handling -v

No MySQL server, network or live `recroot` database is used or needed. The
database is replaced by FakeConn below. Real modules are used wherever they
can be imported; only modules that are NOT importable in the current
environment (pymysql, or project files not present) get a minimal stand-in.
"""

import importlib
import importlib.util
import os
import sys
import threading
import time
import types
import unittest
from datetime import date, datetime
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Stand-ins for modules that may not be importable here
# ---------------------------------------------------------------------------

def _importable(name):
    if name in sys.modules:           # already imported or already stood in for: never replace it
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _install(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


if not _importable("pymysql"):
    class _MySQLError(Exception):
        pass

    class _DatabaseError(_MySQLError):
        pass

    class _OperationalError(_DatabaseError):
        pass

    class _InterfaceError(_MySQLError):
        pass

    _err = _install("pymysql.err", MySQLError=_MySQLError, DatabaseError=_DatabaseError,
                    OperationalError=_OperationalError, InterfaceError=_InterfaceError)
    _cursors = _install("pymysql.cursors", DictCursor=object)
    _pymysql = _install("pymysql", err=_err, cursors=_cursors,
                        connect=lambda **kw: None)
import pymysql  # noqa: E402  (real or stand-in)
import pymysql.err  # noqa: E402

OperationalError = pymysql.err.OperationalError


def deadlock():
    return OperationalError(1213, "Deadlock found when trying to get lock; try restarting transaction")


def lock_timeout():
    return OperationalError(1205, "Lock wait timeout exceeded; try restarting transaction")


if not _importable("modules.consistency_checker"):
    _install("modules.consistency_checker",
             application_consistency_fields=lambda r: (False, None),
             check_consistency=lambda *a, **k: {})
if not _importable("modules.scoring_engine"):
    _install("modules.scoring_engine",
             save_score=lambda *a, **k: None,
             score_application=lambda *a, **k: {"scored": False, "reason": "stub"})
try:
    _have_parser = _importable("services.resume_parser")
except Exception:
    _have_parser = False
if not _have_parser:
    _install("services", __path__=[])
    _install("services.resume_parser", ResumeParseError=type("ResumeParseError", (Exception,), {}),
             parse_resume=lambda path: None)
if not _importable("modules.admin_analytics"):
    _install("modules.admin_analytics", get_analytics=lambda db: {})
if not _importable("modules.job_service"):
    _install("modules.job_service", VALID_JOB_STATUSES=("open", "closed"),
             get_recruiter=None, insert_job=None, parse_and_validate_job_form=None,
             set_job_status=None)
if not _importable("modules.scheduler_view"):
    _install("modules.scheduler_view", build_scheduler_view=lambda snap: {})

from modules import db_retry, deadlock_demo, lock_manager, scheduler, worker  # noqa: E402
from modules.db_retry import lock_events  # noqa: E402
from modules.lock_manager import (  # noqa: E402
    LockAcquireTimeout, LockOrderError, RankedLock, acquire_in_order, held_lock_names,
)


# ---------------------------------------------------------------------------
# Fake database
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append((" ".join(sql.split()), params))
        if self.conn.plan:
            error = self.conn.plan.pop(0)
            if error is not None:
                raise error
        return 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, plan=None):
        self.plan = plan if plan is not None else []
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.log = None            # optional shared event list

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        if self.log is not None:
            self.log.append("commit")

    def rollback(self):
        self.rollbacks += 1

    def ping(self, reconnect=True):
        return None

    def close(self):
        self.closed = True


class FakeAdminDB:
    """Stands in for the request's MySQL connection on the admin page. Answers
    the two read-only queries the dashboard makes and records everything it was
    asked, so tests can prove nothing was written."""

    def __init__(self, counts=None, failures=None, fail_with=None):
        self.counts = counts if counts is not None else [
            {"processing_status": "done", "total": 5},
            {"processing_status": "failed", "total": 1},
        ]
        self.failures = failures if failures is not None else []
        self.fail_with = fail_with
        self.statements = []
        self.commits = 0
        self._last = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.statements.append(sql)
        if self.fail_with is not None:
            raise self.fail_with
        self._last = sql

    def fetchall(self):
        if "GROUP BY processing_status" in (self._last or ""):
            return list(self.counts)
        if "system_process_failed" in (self._last or ""):
            return list(self.failures)
        return []

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def make_pool(**kwargs):
    defaults = dict(db_config={"host": "x", "port": 1, "user": "u", "password": "p",
                               "database": "recroot_FAKE"},
                    upload_folder=".", pool_size=1, queue_max_size=50,
                    max_retries=2, retry_backoff=0.5)
    defaults.update(kwargs)
    return worker.ResumeWorkerPool(**defaults)


# ===========================================================================
# 1. lock_manager
# ===========================================================================

class RankedLockTests(unittest.TestCase):

    def test_ascending_order_is_allowed_and_tracked(self):
        a, b = RankedLock("a", 1), RankedLock("b", 2)
        with a:
            with b:
                self.assertEqual(held_lock_names(), ("a", "b"))
        self.assertEqual(held_lock_names(), ())
        self.assertFalse(a.locked() or b.locked())

    def test_descending_order_is_refused_immediately_without_blocking(self):
        a, b = RankedLock("a", 1), RankedLock("b", 2)
        with b:
            started = time.monotonic()
            with self.assertRaises(LockOrderError):
                a.acquire(True, 5)                       # would wait 5 s if it blocked
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(a.locked())                 # nothing was acquired

    def test_equal_rank_and_reacquire_are_refused(self):
        a, b = RankedLock("a", 5), RankedLock("b", 5)
        with a:
            with self.assertRaises(LockOrderError):
                b.acquire()
            with self.assertRaises(LockOrderError):
                a.acquire()

    def test_try_lock_is_exempt_from_ordering(self):
        a, b = RankedLock("a", 1), RankedLock("b", 2)
        with b:
            self.assertTrue(a.acquire(blocking=False))   # cannot wait, cannot deadlock
            a.release()

    def test_acquire_timeout_returns_false_and_leaves_no_trace(self):
        a = RankedLock("a", 1)
        holder_ready, release_it = threading.Event(), threading.Event()

        def hold():
            with a:
                holder_ready.set()
                release_it.wait(5)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        holder_ready.wait(2)
        try:
            self.assertFalse(a.acquire(True, 0.05))
            self.assertEqual(held_lock_names(), ())
        finally:
            release_it.set()
            t.join(2)

    def test_default_timeout_raises_lock_acquire_timeout(self):
        a = RankedLock("a", 1, default_timeout=0.05)
        holder_ready, release_it = threading.Event(), threading.Event()

        def hold():
            with a:
                holder_ready.set()
                release_it.wait(5)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        holder_ready.wait(2)
        try:
            with self.assertRaises(LockAcquireTimeout):
                with a:
                    pass
        finally:
            release_it.set()
            t.join(2)

    def test_release_of_unheld_lock_raises(self):
        with self.assertRaises(RuntimeError):
            RankedLock("a", 1).release()

    def test_order_is_tracked_per_thread(self):
        high, low = RankedLock("high", 30), RankedLock("low", 10)
        outcome = []

        def other():
            with low:                                   # this thread holds nothing else
                outcome.append("ok")

        with high:
            t = threading.Thread(target=other)
            t.start()
            t.join(2)
        self.assertEqual(outcome, ["ok"])

    def test_works_as_condition_lock(self):
        cond = threading.Condition(RankedLock("c", 1))
        got = []

        def waiter():
            with cond:
                cond.wait_for(lambda: got, timeout=2)
                got.append(held_lock_names())

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        with cond:
            got.append("go")
            cond.notify()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(got[-1], ("c",))               # lock re-held after wait()

    def test_condition_wait_timeout_keeps_lock_owned(self):
        cond = threading.Condition(RankedLock("c", 1))
        with cond:
            self.assertFalse(cond.wait(0.05))
            self.assertEqual(held_lock_names(), ("c",))
        self.assertEqual(held_lock_names(), ())


class AcquireInOrderTests(unittest.TestCase):

    def test_sorts_by_rank_whatever_the_request_order(self):
        a, b, c = RankedLock("a", 1), RankedLock("b", 2), RankedLock("c", 3)
        with acquire_in_order([c, a, b]) as order:
            self.assertEqual([l.name for l in order], ["a", "b", "c"])
            self.assertEqual(held_lock_names(), ("a", "b", "c"))
        self.assertEqual(held_lock_names(), ())

    def test_failed_attempt_releases_everything_it_took(self):
        a, b = RankedLock("a", 1), RankedLock("b", 2)
        holder_ready, release_it = threading.Event(), threading.Event()

        def hold_b():
            with b:
                holder_ready.set()
                release_it.wait(5)

        t = threading.Thread(target=hold_b, daemon=True)
        t.start()
        holder_ready.wait(2)
        try:
            with self.assertRaises(LockAcquireTimeout):
                with acquire_in_order([a, b], timeout=0.1):
                    self.fail("should not get here")
            self.assertFalse(a.locked())                 # A was released again
            self.assertEqual(held_lock_names(), ())
        finally:
            release_it.set()
            t.join(2)

    def test_duplicates_collapse_and_equal_ranks_rejected(self):
        a, b = RankedLock("a", 1), RankedLock("b", 1)
        with acquire_in_order([a, a]):
            pass
        with self.assertRaises(ValueError):
            with acquire_in_order([a, b]):
                pass

    def test_two_threads_with_opposite_request_orders_never_deadlock(self):
        a, b = RankedLock("a", 1), RankedLock("b", 2)
        errors, done = [], []

        def run(order):
            try:
                for _ in range(200):
                    with acquire_in_order(order, timeout=5):
                        pass
                done.append(1)
            except Exception as exc:                     # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run, args=([a, b],), daemon=True),
                   threading.Thread(target=run, args=([b, a],), daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(done), 2)


# ===========================================================================
# 2. db_retry
# ===========================================================================

class ClassificationTests(unittest.TestCase):

    def test_codes(self):
        self.assertTrue(db_retry.is_deadlock(deadlock()))
        self.assertFalse(db_retry.is_lock_timeout(deadlock()))
        self.assertTrue(db_retry.is_lock_timeout(lock_timeout()))
        self.assertTrue(db_retry.is_lock_error(deadlock()))
        self.assertTrue(db_retry.is_lock_error(lock_timeout()))

    def test_other_errors_are_not_lock_errors(self):
        self.assertFalse(db_retry.is_lock_error(OperationalError(1064, "syntax")))
        self.assertFalse(db_retry.is_lock_error(OperationalError(2013, "lost connection")))
        self.assertFalse(db_retry.is_lock_error(ValueError("x")))

    def test_builtin_exceptions_with_a_matching_number_are_ignored(self):
        self.assertFalse(db_retry.is_lock_error(KeyError(1213)))
        self.assertFalse(db_retry.is_lock_error(Exception(1213)))   # builtins module

    def test_errno_attribute_style_drivers(self):
        class ConnectorError(Exception):
            def __init__(self, errno):
                super().__init__("boom")
                self.errno = errno
        self.assertTrue(db_retry.is_deadlock(ConnectorError(1213)))
        self.assertFalse(db_retry.is_lock_error(ConnectorError(1062)))


class BackoffTests(unittest.TestCase):

    def test_exponential_growth_and_cap(self):
        values = [db_retry.backoff_delay(n, base=0.1, cap=1.0, jitter=0.0) for n in range(1, 7)]
        self.assertEqual(values, [0.1, 0.2, 0.4, 0.8, 1.0, 1.0])

    def test_jitter_stays_inside_bounds_and_never_exceeds_cap(self):
        for attempt in range(1, 9):
            raw = min(2.0, 0.1 * 2 ** (attempt - 1))
            for rng in (0.0, 0.5, 0.999999):
                d = db_retry.backoff_delay(attempt, base=0.1, cap=2.0, jitter=0.25, rng=lambda r=rng: r)
                self.assertGreaterEqual(d, raw * 0.75 - 1e-12)
                self.assertLessEqual(d, raw + 1e-12)

    def test_attempt_must_start_at_one(self):
        with self.assertRaises(ValueError):
            db_retry.backoff_delay(0)


class RunWithLockRetryTests(unittest.TestCase):

    def setUp(self):
        self.counters = db_retry.LockEventCounters()
        self.sleeps = []
        self.rollbacks = []

    def run_op(self, operation, **kw):
        return db_retry.run_with_lock_retry(
            operation, lambda: self.rollbacks.append(1), sleep=self.sleeps.append,
            counters=self.counters, jitter=0.0, base_delay=0.1, max_delay=1.0, **kw)

    def test_succeeds_after_deadlock_with_rollback_before_each_retry(self):
        calls = []

        def op():
            calls.append(1)
            if len(calls) < 3:
                raise deadlock()
            return "done"

        self.assertEqual(self.run_op(op, max_retries=3), "done")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.rollbacks), 2)
        self.assertEqual(self.sleeps, [0.1, 0.2])
        snap = self.counters.snapshot()
        self.assertEqual((snap["deadlocks"], snap["retries"], snap["recovered"], snap["exhausted"]),
                         (2, 2, 1, 0))

    def test_lock_wait_timeout_is_retried_too(self):
        calls = []

        def op():
            calls.append(1)
            if len(calls) == 1:
                raise lock_timeout()
            return "ok"

        self.assertEqual(self.run_op(op), "ok")
        snap = self.counters.snapshot()
        self.assertEqual((snap["lock_timeouts"], snap["deadlocks"], snap["recovered"]), (1, 0, 1))

    def test_retries_are_bounded_and_original_error_is_reraised(self):
        calls = []

        def op():
            calls.append(1)
            raise deadlock()

        with self.assertRaises(OperationalError) as ctx:
            self.run_op(op, max_retries=2)
        self.assertEqual(ctx.exception.args[0], 1213)
        self.assertEqual(len(calls), 3)                  # 1 try + 2 retries, then stop
        self.assertEqual(len(self.rollbacks), 3)         # rolled back every time, last included
        snap = self.counters.snapshot()
        self.assertEqual((snap["deadlocks"], snap["retries"], snap["exhausted"], snap["recovered"]),
                         (3, 2, 1, 0))

    def test_max_retries_is_hard_capped(self):
        calls = []

        def op():
            calls.append(1)
            raise deadlock()

        with self.assertRaises(OperationalError):
            self.run_op(op, max_retries=10_000)
        self.assertEqual(len(calls), db_retry.MAX_RETRIES_LIMIT + 1)

    def test_other_errors_propagate_immediately_without_retry(self):
        calls = []

        def op():
            calls.append(1)
            raise OperationalError(1064, "syntax error")

        with self.assertRaises(OperationalError):
            self.run_op(op)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.counters.snapshot()["total_mysql_events"], 0)

    def test_failing_rollback_does_not_mask_the_retry(self):
        calls = []

        def op():
            calls.append(1)
            if len(calls) == 1:
                raise deadlock()
            return "ok"

        def bad_rollback():
            raise OperationalError(2006, "gone")

        result = db_retry.run_with_lock_retry(op, bad_rollback, sleep=lambda s: None,
                                              counters=self.counters, jitter=0.0)
        self.assertEqual(result, "ok")

    def test_zero_retries_means_a_single_attempt(self):
        with self.assertRaises(OperationalError):
            self.run_op(lambda: (_ for _ in ()).throw(deadlock()), max_retries=0)
        self.assertEqual(self.sleeps, [])


class CounterTests(unittest.TestCase):

    def test_record_error_ignores_non_lock_errors(self):
        c = db_retry.LockEventCounters()
        self.assertIsNone(c.record_error(OperationalError(1064, "x")))
        self.assertIsNone(c.record_error(KeyError(1213)))
        self.assertEqual(c.snapshot()["total_mysql_events"], 0)
        self.assertIsNone(c.snapshot()["last_event"])

    def test_last_event_details(self):
        c = db_retry.LockEventCounters()
        self.assertEqual(c.record_error(deadlock(), where="application #9"), "deadlock")
        last = c.snapshot()["last_event"]
        self.assertEqual((last["kind"], last["code"], last["where"]), ("deadlock", 1213, "application #9"))

    def test_thread_safe_counting(self):
        c = db_retry.LockEventCounters()
        threads = [threading.Thread(target=lambda: [c.record_error(deadlock()) for _ in range(500)])
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(c.snapshot()["deadlocks"], 4000)


# ===========================================================================
# 3. worker integration
# ===========================================================================

class WorkerRetryTests(unittest.TestCase):

    def setUp(self):
        lock_events.reset()
        self.pool = make_pool()
        self.plan = []
        self.conns = []

        def open_conn():
            conn = FakeConn(self.plan)
            self.conns.append(conn)
            return conn

        patches = [mock.patch.object(self.pool, "_open_connection", side_effect=open_conn),
                   mock.patch.object(worker.time, "sleep")]
        self.sleep = patches[1].start()
        patches[0].start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(lock_events.reset)

    def test_deadlock_is_rolled_back_and_retried_with_bounded_jittered_backoff(self):
        attempts = []

        def once(db, app_id):
            attempts.append(1)
            if len(attempts) <= 2:
                raise deadlock()
            return worker.STATE_DONE, None

        self.pool._process_once = once
        self.assertEqual(self.pool._process_with_retries(7), (worker.STATE_DONE, None))
        self.assertEqual(len(attempts), 3)

        delays = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(len(delays), 2)
        self.assertTrue(0.375 <= delays[0] <= 0.5, delays)      # base 0.5, jitter 25 %
        self.assertTrue(0.75 <= delays[1] <= 1.0, delays)       # doubled
        self.assertTrue(all(c.rollbacks >= 1 for c in self.conns[:2]),
                        "each failed transaction must be rolled back")
        self.assertTrue(self.conns[0].closed and self.conns[1].closed,
                        "a failed connection is discarded, not reused")

        snap = lock_events.snapshot()
        self.assertEqual((snap["deadlocks"], snap["retries"], snap["recovered"], snap["exhausted"]),
                         (2, 2, 1, 0))
        self.assertEqual(self.pool.get_stats()["retries"], 2)   # existing counter still works

    def test_lock_wait_timeout_is_retried(self):
        attempts = []

        def once(db, app_id):
            attempts.append(1)
            if len(attempts) == 1:
                raise lock_timeout()
            return worker.STATE_DONE, None

        self.pool._process_once = once
        self.assertEqual(self.pool._process_with_retries(7)[0], worker.STATE_DONE)
        snap = lock_events.snapshot()
        self.assertEqual((snap["lock_timeouts"], snap["deadlocks"], snap["recovered"]), (1, 0, 1))

    def test_retries_exhausted_marks_failed_and_audits(self):
        attempts = []

        def once(db, app_id):
            attempts.append(1)
            raise deadlock()

        self.pool._process_once = once
        state, note = self.pool._process_with_retries(7)
        self.assertEqual(state, worker.STATE_FAILED)
        self.assertIn("OperationalError", note)
        self.assertEqual(len(attempts), 3)                      # max_retries=2 -> 3 attempts
        self.assertEqual(self.sleep.call_count, 2)

        snap = lock_events.snapshot()
        self.assertEqual((snap["deadlocks"], snap["retries"], snap["exhausted"], snap["recovered"]),
                         (3, 2, 1, 0))

        statements = [s for c in self.conns for s in c.statements]
        self.assertTrue(any(s[0].startswith("UPDATE applications SET processing_status") for s in statements))
        self.assertTrue(any("INSERT INTO audit_log" in s[0] for s in statements))
        self.assertGreaterEqual(sum(c.commits for c in self.conns), 1)

    def test_non_lock_database_error_is_not_retried(self):
        def once(db, app_id):
            raise OperationalError(1064, "You have an error in your SQL syntax")

        self.pool._process_once = once
        self.assertEqual(self.pool._process_with_retries(7)[0], worker.STATE_FAILED)
        self.assertEqual(self.sleep.call_count, 0)
        self.assertEqual(lock_events.snapshot()["total_mysql_events"], 0)

    def test_lost_connection_still_retried_exactly_as_before(self):
        attempts = []

        def once(db, app_id):
            attempts.append(1)
            if len(attempts) == 1:
                raise OperationalError(2013, "Lost connection to MySQL server")
            return worker.STATE_DONE, None

        self.pool._process_once = once
        self.assertEqual(self.pool._process_with_retries(7)[0], worker.STATE_DONE)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.5])   # no jitter here
        snap = lock_events.snapshot()
        self.assertEqual((snap["deadlocks"], snap["lock_timeouts"], snap["retries"]), (0, 0, 0))
        self.assertEqual(self.pool.get_stats()["retries"], 1)

    def test_lock_acquire_timeout_is_retried_and_counted_separately(self):
        attempts = []

        def once(db, app_id):
            attempts.append(1)
            if len(attempts) == 1:
                raise LockAcquireTimeout("worker.dedup")
            return worker.STATE_DONE, None

        self.pool._process_once = once
        self.assertEqual(self.pool._process_with_retries(7)[0], worker.STATE_DONE)
        snap = lock_events.snapshot()
        self.assertEqual((snap["python_lock_timeouts"], snap["deadlocks"], snap["total_mysql_events"]),
                         (1, 0, 0))

    def test_record_failure_itself_survives_a_deadlock(self):
        self.plan.extend([deadlock()])                  # first statement it runs deadlocks
        self.pool._record_failure(7, "boom")
        statements = [s[0] for c in self.conns for s in c.statements]
        updates = [s for s in statements if s.startswith("UPDATE applications SET processing_status")]
        self.assertEqual(len(updates), 2)               # failed once, then succeeded
        self.assertEqual(sum("INSERT INTO audit_log" in s for s in statements), 1)
        self.assertEqual(sum(c.commits for c in self.conns), 1)
        self.assertGreaterEqual(sum(c.rollbacks for c in self.conns), 1)
        snap = lock_events.snapshot()
        self.assertEqual((snap["deadlocks"], snap["recovered"]), (1, 1))


class WorkerPipelineLockTests(unittest.TestCase):
    """_process_once with the DB and the stage functions replaced."""

    def setUp(self):
        self.pool = make_pool()
        self.log = []
        self.conn = FakeConn()
        self.conn.log = self.log
        application = {"candidate_id": 3, "resume_path": "r.pdf",
                       "candidate_name": "A", "candidate_email": "a@x"}
        self.pool._load_application = lambda db, aid: (application, {"id": 1}, "hash", True)
        self.pool._parse_resume_file = lambda path: ({"resume_text": "text"}, None)
        patches = [
            mock.patch.object(worker, "index_resume", lambda *a, **k: self.log.append("index")),
            mock.patch.object(worker, "application_duplicate_fields", lambda r: (False, 0)),
            mock.patch.object(worker, "check_consistency", lambda *a, **k: {}),
            mock.patch.object(worker, "application_consistency_fields", lambda r: (False, None)),
            mock.patch.object(worker, "score_application",
                              lambda *a, **k: {"scored": False, "reason": "stub"}),
            mock.patch.object(worker, "save_score", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)

    def test_read_only_transaction_is_ended_inside_the_dedup_stage(self):
        def check(*a, **k):
            self.log.append("check")
            return {}

        with mock.patch.object(worker, "check_duplicates", check):
            self.assertEqual(self.pool._process_once(self.conn, 7)[0], worker.STATE_DONE)
        # processing-status commit, [index, commit, check, commit], final commit
        self.assertEqual(self.log[:6], ["commit", "index", "commit", "check", "commit", "commit"])

    def test_dedup_lock_is_released_when_the_stage_hits_a_deadlock(self):
        def check(*a, **k):
            raise deadlock()

        with mock.patch.object(worker, "check_duplicates", check):
            with self.assertRaises(OperationalError):
                self.pool._process_once(self.conn, 7)
        self.assertFalse(self.pool._dedup_lock.locked())
        self.assertEqual(held_lock_names(), ())

    def test_dedup_lock_timeout_propagates_for_retry_instead_of_becoming_a_stage_error(self):
        pool = make_pool(dedup_lock_timeout=0.05)
        application = {"candidate_id": 3, "resume_path": "r.pdf",
                       "candidate_name": "A", "candidate_email": "a@x"}
        pool._load_application = lambda db, aid: (application, {"id": 1}, "hash", True)
        pool._parse_resume_file = lambda path: ({"resume_text": "text"}, None)
        holding, release = threading.Event(), threading.Event()

        def hog():
            with pool._dedup_lock:
                holding.set()
                release.wait(5)

        t = threading.Thread(target=hog, daemon=True)
        t.start()
        holding.wait(2)
        try:
            with mock.patch.object(worker, "check_duplicates", lambda *a, **k: {}):
                with self.assertRaises(LockAcquireTimeout):
                    pool._process_once(self.conn, 7)
        finally:
            release.set()
            t.join(2)
        # nothing was written as a (permanent) failure by the final transaction
        self.assertFalse(any("INSERT INTO audit_log" in s[0] for s in self.conn.statements))

    def test_final_transaction_writes_applications_before_audit_log(self):
        with mock.patch.object(worker, "check_duplicates", lambda *a, **k: {}):
            self.pool._process_once(self.conn, 7)
        sql = [s[0] for s in self.conn.statements]
        update_idx = max(i for i, s in enumerate(sql) if s.startswith("UPDATE applications SET"))
        audit_idx = max(i for i, s in enumerate(sql) if "INSERT INTO audit_log" in s)
        self.assertLess(update_idx, audit_idx)


class WorkerWiringTests(unittest.TestCase):

    def test_locks_have_the_documented_ranks_and_the_dedup_lock_is_bounded(self):
        pool = make_pool(dedup_lock_timeout=33)
        self.assertEqual(pool._dedup_lock.rank, lock_manager.RANK_DEDUP)
        self.assertEqual(pool._state_lock.rank, lock_manager.RANK_STATE)
        self.assertEqual(pool._scheduler._cond._lock.rank, lock_manager.RANK_SCHEDULER)
        self.assertEqual(pool._dedup_lock.default_timeout, 33)
        self.assertIsNone(pool._state_lock.default_timeout)
        self.assertTrue(lock_manager.RANK_DEDUP < lock_manager.RANK_STATE < lock_manager.RANK_SCHEDULER)

    def test_get_stats_nesting_state_then_scheduler_is_legal(self):
        stats = make_pool().get_stats()                  # raises LockOrderError if the order broke
        self.assertEqual(stats["queue_depth"], 0)
        self.assertEqual(stats["scheduling_algorithm"], "fcfs")

    def test_connection_gets_a_bounded_lock_wait(self):
        for configured, expected in ((None, None), (0, None), (10, 10), (99999, 3600), (-4, 1)):
            pool = make_pool(lock_wait_timeout=configured)
            with mock.patch.object(pymysql, "connect") as connect:
                pool._open_connection()
            kwargs = connect.call_args.kwargs
            if expected is None:
                self.assertNotIn("init_command", kwargs)
            else:
                self.assertEqual(kwargs["init_command"],
                                 f"SET SESSION innodb_lock_wait_timeout = {expected}")
        self.assertEqual(worker.DEFAULT_LOCK_WAIT_TIMEOUT, 10)

    def test_enqueue_and_snapshot_work_with_ranked_locks(self):
        pool = make_pool()
        pool._accepting = True                            # producer path only, no threads
        meta = {"applied_at": datetime(2026, 1, 1, 9, 0, 0), "deadline": None, "job_title": "J"}
        self.assertTrue(pool.enqueue(1, meta))
        self.assertFalse(pool.enqueue(1, meta))           # already in flight
        snap = pool.get_scheduler_snapshot()
        self.assertEqual([r["application_id"] for r in snap["tasks"]], [1])
        stats = pool.get_stats()
        self.assertEqual((stats["enqueued"], stats["rejected_in_flight"]), (1, 1))

    def test_real_threads_complete_every_task_even_when_each_first_try_deadlocks(self):
        lock_events.reset()
        self.addCleanup(lock_events.reset)
        pool = make_pool(pool_size=4, max_retries=2, retry_backoff=0.001)
        plan_lock = threading.Lock()
        seen = {}

        def once(db, app_id):
            with plan_lock:
                seen[app_id] = seen.get(app_id, 0) + 1
                first = seen[app_id] == 1
            if first and app_id % 2:
                raise deadlock()
            return worker.STATE_DONE, None

        pool._process_once = once
        with mock.patch.object(pool, "_open_connection", side_effect=lambda: FakeConn()):
            pool.start()
            try:
                ids = list(range(1, 41))
                meta = {"applied_at": datetime(2026, 1, 1), "deadline": None, "job_title": ""}

                def produce(chunk):
                    for i in chunk:
                        self.assertTrue(pool.enqueue(i, meta))

                producers = [threading.Thread(target=produce, args=(ids[k::4],)) for k in range(4)]
                for p in producers:
                    p.start()
                for p in producers:
                    p.join(5)
                end = time.monotonic() + 20
                while time.monotonic() < end and pool.get_stats()["completed"] < len(ids):
                    time.sleep(0.02)
                stats = pool.get_stats()
            finally:
                pool.stop(timeout=5)

        odd = len([i for i in ids if i % 2])
        self.assertEqual((stats["completed"], stats["failed"]), (40, 0))
        snap = lock_events.snapshot()
        self.assertEqual((snap["deadlocks"], snap["recovered"], snap["exhausted"]), (odd, odd, 0))
        self.assertEqual([t for t in pool._threads if t.is_alive()], [])


# ===========================================================================
# 4. Scheduling behaviour is unchanged by the ranked lock
# ===========================================================================

class SchedulingUnchangedTests(unittest.TestCase):
    TODAY = date(2026, 6, 15)

    def tasks(self):
        d = datetime
        return [
            scheduler.make_task(1, d(2026, 6, 1, 9), date(2026, 7, 30), "late deadline"),
            scheduler.make_task(2, d(2026, 6, 1, 10), None, "no deadline"),
            scheduler.make_task(3, d(2026, 6, 1, 11), date(2026, 6, 20), "soon"),
            scheduler.make_task(4, d(2026, 6, 1, 12), date(2026, 6, 1), "expired"),
            scheduler.make_task(5, d(2026, 6, 1, 13), date(2026, 6, 15), "due today"),
            scheduler.make_task(6, d(2026, 6, 1, 9), date(2026, 6, 20), "soon, earlier applied"),
        ]

    def drain(self, queue):
        out = []
        for task in self.tasks():
            self.assertEqual(queue.add(task), scheduler.ADD_QUEUED)
        for _ in range(len(self.tasks())):
            d = queue.pop_next(timeout=1)
            out.append((d.task.application_id, d.algorithm, d.dispatch_seq,
                        d.queue_size, d.passed_over, d.bucket))
        return out

    def test_identical_decisions_with_plain_and_ranked_locks(self):
        for algorithm in scheduler.ALGORITHMS:
            plain = scheduler.SchedulerQueue(capacity=10, algorithm=algorithm,
                                             today_func=lambda: self.TODAY)
            ranked = scheduler.SchedulerQueue(capacity=10, algorithm=algorithm,
                                              today_func=lambda: self.TODAY,
                                              lock=RankedLock("scheduler.queue", lock_manager.RANK_SCHEDULER))
            self.assertEqual(self.drain(plain), self.drain(ranked), algorithm)

    def test_expected_orders_through_the_worker_pools_own_queue(self):
        expected = {
            scheduler.ALGORITHM_FCFS: [1, 6, 2, 3, 4, 5],
            scheduler.ALGORITHM_PRIORITY: [5, 6, 3, 1, 2, 4],
        }
        for algorithm, order in expected.items():
            pool = make_pool(scheduling_algorithm=algorithm)
            pool._scheduler._today_func = lambda: self.TODAY
            got = [row[0] for row in self.drain(pool._scheduler)]
            self.assertEqual(got, order, algorithm)

    def test_switching_algorithm_at_run_time_still_works(self):
        pool = make_pool()
        self.assertEqual(pool.set_scheduling_algorithm("priority"), "fcfs")
        self.assertEqual(pool.get_scheduling_algorithm(), "priority")
        with self.assertRaises(ValueError):
            pool.set_scheduling_algorithm("nonsense")

    def test_blocked_consumer_wakes_when_a_task_arrives(self):
        queue = scheduler.SchedulerQueue(capacity=5, lock=RankedLock("q", lock_manager.RANK_SCHEDULER))
        got = []
        t = threading.Thread(target=lambda: got.append(queue.pop_next(timeout=3)))
        t.start()
        time.sleep(0.1)
        queue.add(scheduler.make_task(9, datetime(2026, 1, 1)))
        t.join(3)
        self.assertEqual(got[0].task.application_id, 9)


# ===========================================================================
# 5. Demo module
# ===========================================================================

class DemoTests(unittest.TestCase):

    def setUp(self):
        lock_events.reset()
        self.addCleanup(lock_events.reset)
        self.threads_before = threading.active_count()

    def assertNoDemoThreadsLeft(self):
        names = [t.name for t in threading.enumerate() if t.name.startswith("demo-")]
        self.assertEqual(names, [])

    def test_simulation_reproduces_a_circular_wait_and_ends_by_timeout(self):
        started = time.monotonic()
        result = deadlock_demo.simulate_deadlock(timeout=0.3)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(result["outcome"], "deadlock_detected")
        self.assertTrue(result["simulated"])
        self.assertFalse(result["database_involved"])
        self.assertTrue(result["cleaned_up"])
        timed_out = {e["actor"] for e in result["events"] if "TIMED OUT" in e["message"]}
        self.assertEqual(timed_out, {"Thread 1", "Thread 2"})
        self.assertNoDemoThreadsLeft()

    def test_simulation_is_deterministic_over_repeated_runs(self):
        for _ in range(8):
            self.assertEqual(deadlock_demo.simulate_deadlock(timeout=0.2)["outcome"],
                             "deadlock_detected")
        self.assertNoDemoThreadsLeft()

    def test_prevention_demo_completes_and_rejects_the_wrong_order(self):
        for _ in range(5):
            result = deadlock_demo.run_prevention_demo(timeout=0.5)
            self.assertEqual(result["outcome"], "completed_without_deadlock", result["summary"])
            self.assertTrue(result["order_violation_detected"])
            self.assertTrue(result["simulated"])
            self.assertFalse(result["database_involved"])
            self.assertTrue(result["cleaned_up"])
            self.assertTrue(any("REFUSED" in e["message"] for e in result["events"]))
        self.assertNoDemoThreadsLeft()

    def test_demos_never_change_the_real_counters(self):
        before = lock_events.snapshot()
        deadlock_demo.simulate_deadlock(timeout=0.2)
        deadlock_demo.run_prevention_demo(timeout=0.3)
        self.assertEqual(lock_events.snapshot(), before)
        self.assertEqual(before["total_mysql_events"], 0)

    def test_demos_use_no_database(self):
        with mock.patch.object(pymysql, "connect", side_effect=AssertionError("DB touched")):
            deadlock_demo.simulate_deadlock(timeout=0.2)
            deadlock_demo.run_prevention_demo(timeout=0.3)

    def test_second_request_while_one_is_running_gets_busy(self):
        self.assertTrue(deadlock_demo._run_gate.acquire(blocking=False))
        try:
            self.assertEqual(deadlock_demo.simulate_deadlock(0.2)["outcome"], "busy")
            self.assertEqual(deadlock_demo.run_prevention_demo(0.2)["outcome"], "busy")
        finally:
            deadlock_demo._run_gate.release()
        self.assertEqual(deadlock_demo.run_prevention_demo(0.3)["outcome"],
                         "completed_without_deadlock")

    def test_timeout_is_clamped_so_a_demo_cannot_run_long(self):
        clamp = deadlock_demo._clamp
        self.assertEqual(clamp(999), deadlock_demo.MAX_TIMEOUT)
        self.assertEqual(clamp(-3), deadlock_demo.MIN_TIMEOUT)
        self.assertEqual(clamp("abc"), deadlock_demo.DEFAULT_TIMEOUT)
        self.assertEqual(clamp(float("nan")), deadlock_demo.DEFAULT_TIMEOUT)
        self.assertEqual(clamp(None), deadlock_demo.DEFAULT_TIMEOUT)


# ===========================================================================
# 6. Admin routes (Flask test client; no database, no CSRF extension)
# ===========================================================================

def _make_app():
    from flask import Flask
    from modules.admin import admin_bp
    from modules.auth import auth_bp

    app = Flask("deadlock_tests", template_folder=os.path.join(ROOT, "templates"))
    app.secret_key = "test"
    app.config["TESTING"] = True
    app.jinja_env.globals["csrf_token"] = lambda: "test-token"
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    return app


class AdminDeadlockPageTests(unittest.TestCase):

    def setUp(self):
        lock_events.reset()
        self.addCleanup(lock_events.reset)
        self.client = _make_app().test_client()
        from modules import admin
        self.admin = admin
        real_sim, real_prev = deadlock_demo.simulate_deadlock, deadlock_demo.run_prevention_demo
        self.sim = mock.Mock(side_effect=lambda: real_sim(0.3))
        self.prev = mock.Mock(side_effect=lambda: real_prev(0.4))
        self.db = FakeAdminDB()
        self.open_db = mock.Mock(return_value=self.db)
        mock.patch.object(admin, "_open_db", self.open_db).start()
        mock.patch.object(admin, "simulate_deadlock", self.sim).start()
        mock.patch.object(admin, "run_prevention_demo", self.prev).start()
        self.addCleanup(mock.patch.stopall)

    def login(self, role):
        with self.client.session_transaction() as s:
            s.update(user_id=1, role=role, name="T", email="t@x")

    def test_only_admins_can_open_or_run_the_demo(self):
        for method, url in (("get", "/admin/deadlock"), ("post", "/admin/deadlock/simulate"),
                            ("post", "/admin/deadlock/prevention")):
            r = getattr(self.client, method)(url)                  # anonymous
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"])
        for role in ("candidate", "recruiter"):
            self.login(role)
            for method, url in (("get", "/admin/deadlock"), ("post", "/admin/deadlock/simulate"),
                                ("post", "/admin/deadlock/prevention")):
                self.assertEqual(getattr(self.client, method)(url).status_code, 302, (role, url))
        self.sim.assert_not_called()
        self.prev.assert_not_called()

    def test_page_has_both_buttons_and_labels_simulation_vs_real(self):
        self.login("admin")
        r = self.client.get("/admin/deadlock")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        for text in ("Simulate Deadlock", "Run Prevention Demo", "SIMULATION ONLY",
                     "Real MySQL lock events", "No MySQL deadlock or lock wait timeout"):
            self.assertIn(text, html)
        self.assertNotIn('id="demo-result"', html)                 # nothing simulated yet
        self.assertIn("/admin/deadlock/simulate", html)
        self.assertIn("/admin/deadlock/prevention", html)
        self.assertIn('name="csrf_token"', html)

    def test_simulate_button_shows_a_result_labelled_simulated_not_real(self):
        self.login("admin")
        r = self.client.post("/admin/deadlock/simulate")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="demo-result"', html)
        self.assertIn("SIMULATED", html)
        self.assertIn("Deadlock reproduced", html)
        self.assertIn("no database", html)
        # the real section is untouched by a simulation
        self.assertIn("No MySQL deadlock or lock wait timeout", html)
        self.assertEqual(lock_events.snapshot()["total_mysql_events"], 0)

    def test_prevention_button_shows_completed_result(self):
        self.login("admin")
        html = self.client.post("/admin/deadlock/prevention").get_data(as_text=True)
        self.assertIn("Both threads completed, no deadlock", html)
        self.assertIn("SIMULATED", html)

    def test_real_events_appear_only_when_real_errors_were_recorded(self):
        self.login("admin")
        lock_events.record_error(deadlock(), where="application #42")
        html = self.client.get("/admin/deadlock").get_data(as_text=True)
        self.assertIn("Deadlock (MySQL 1213)", html)           # row in the real event log
        self.assertIn("application #42", html)
        self.assertNotIn("No MySQL deadlock or lock wait timeout", html)

    def test_admin_nav_links_to_the_page(self):
        self.login("admin")
        self.assertIn('href="/admin/deadlock"', self.client.get("/admin/deadlock").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
