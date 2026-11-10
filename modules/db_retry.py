"""
Recroot - MySQL lock-error handling (deadlock 1213, lock wait timeout 1205)

Pure Python: no Flask, and pymysql is NOT imported (errors are recognised by
their MySQL error code), so this module is unit-testable without a database.

MySQL error codes handled
    1213  ER_LOCK_DEADLOCK      InnoDB found a cycle and rolled this transaction
                                back as the victim. The WHOLE transaction is gone.
    1205  ER_LOCK_WAIT_TIMEOUT  A row lock was not granted within
                                innodb_lock_wait_timeout. By default InnoDB
                                rolls back only the LAST STATEMENT, not the
                                transaction, so an explicit rollback is
                                required before retrying either error.

Retry rules (run_with_lock_retry)
    * The retried unit is the WHOLE transaction: rollback, then run the
      operation again from its first statement. Never retry a single statement.
    * The operation must therefore be idempotent (the worker pipeline is; see
      modules/worker.py).
    * Retries are bounded (max_retries, hard-capped at MAX_RETRIES_LIMIT) with
      exponential backoff capped at max_delay and a little random jitter, so
      two victims of the same deadlock do not wake up in lockstep and collide
      again.
    * Only 1213 and 1205 are retried here. Anything else (bad SQL, constraint
      violation, access denied) propagates immediately.
    * When retries run out, the ORIGINAL exception is re-raised.

Real events only (LockEventCounters)
    The counters are incremented ONLY when a genuine MySQL error 1213/1205
    exception was caught by instrumented code. Nothing is estimated, seeded
    or simulated. They live in this process's memory, reset on restart, and
    only cover code that calls record_error() (currently the worker threads).
    The Deadlock Monitor page labels them accordingly.
"""

import random
import threading
import time
from collections import deque
from datetime import datetime

__all__ = [
    "ER_LOCK_WAIT_TIMEOUT", "ER_LOCK_DEADLOCK", "MAX_RETRIES_LIMIT",
    "mysql_error_code", "is_deadlock", "is_lock_timeout", "is_lock_error",
    "backoff_delay", "LockEventCounters", "lock_events", "run_with_lock_retry",
    "MAX_EVENT_LOG",
]

ER_LOCK_WAIT_TIMEOUT = 1205
ER_LOCK_DEADLOCK = 1213

MAX_RETRIES_LIMIT = 10      # "bounded" really means bounded, whatever the caller passes
MAX_EVENT_LOG = 200         # most recent real events kept in memory
MAX_MESSAGE_LEN = 300       # driver message kept per event


# ============================================================
# Classification
# ============================================================

def mysql_error_code(exc):
    """
    MySQL error code carried by a driver exception, or None.
    PyMySQL puts it in args[0]; mysql-connector exposes .errno. Built-in
    exceptions (KeyError(1213) ...) are never treated as database errors.
    """
    if not isinstance(exc, BaseException) or type(exc).__module__ == "builtins":
        return None
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
        return args[0]
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int) and not isinstance(errno, bool):
        return errno
    return None


def is_deadlock(exc):
    return mysql_error_code(exc) == ER_LOCK_DEADLOCK


def is_lock_timeout(exc):
    return mysql_error_code(exc) == ER_LOCK_WAIT_TIMEOUT


def is_lock_error(exc):
    return mysql_error_code(exc) in (ER_LOCK_DEADLOCK, ER_LOCK_WAIT_TIMEOUT)


# ============================================================
# Backoff
# ============================================================

def backoff_delay(attempt, base=0.1, cap=2.0, jitter=0.25, rng=random.random):
    """
    Seconds to wait after failed attempt number `attempt` (1 = first failure).

    raw = min(cap, base * 2**(attempt-1)); the result is spread over
    [raw * (1 - jitter), raw], so it never exceeds `cap`.
    """
    if attempt < 1:
        raise ValueError("attempt starts at 1.")
    jitter = min(1.0, max(0.0, float(jitter)))
    raw = min(float(cap), float(base) * (2 ** (attempt - 1)))
    return raw * (1.0 - jitter + jitter * rng())


# ============================================================
# Real-event counters
# ============================================================

class LockEventCounters:
    """
    Thread-safe record of real lock events seen by this process: aggregate
    counters plus a bounded log (newest MAX_EVENT_LOG entries) of what actually
    happened. Every entry is created from a genuine exception or a genuine
    outcome of the retry code; there is no seeding, estimation or simulation.
    """

    def __init__(self):
        self._lock = threading.Lock()     # leaf lock: never nested with another
        self._reset_locked()

    def _reset_locked(self):
        self._counts = {
            "deadlocks": 0,             # MySQL 1213 caught
            "lock_timeouts": 0,         # MySQL 1205 caught
            "retries": 0,               # transaction re-runs triggered by those
            "rollbacks": 0,             # explicit rollbacks done after those errors
            "rollback_failures": 0,     # ... that themselves failed (connection discarded)
            "recovered": 0,             # tasks that succeeded after >= 1 such retry
            "exhausted": 0,             # tasks that gave up after the last retry
            "python_lock_timeouts": 0,  # in-process lock not obtained in time
        }
        self._events = deque(maxlen=MAX_EVENT_LOG)
        self._seq = 0
        self._last = None
        self._since = time.time()

    def reset(self):
        """Clear everything (used by tests; never called by the application)."""
        with self._lock:
            self._reset_locked()

    def _append_locked(self, entry):
        self._seq += 1
        entry["seq"] = self._seq
        entry["at"] = time.time()
        entry["thread"] = threading.current_thread().name
        self._events.append(entry)
        return entry

    def record_error(self, exc, where=None, attempt=None, total_attempts=None,
                     retry_in=None, rolled_back=None):
        """
        Record a real MySQL 1213/1205. Returns 'deadlock', 'lock_timeout' or
        None (not a lock error; nothing recorded).

        attempt / total_attempts  which try failed (1-based) out of how many.
        retry_in                  seconds until the retry, or None when no
                                  retry will follow (retries exhausted).
        rolled_back               True/False if a rollback was attempted and
                                  did / did not succeed; None if not known.
        """
        code = mysql_error_code(exc)
        if code == ER_LOCK_DEADLOCK:
            kind, key = "deadlock", "deadlocks"
        elif code == ER_LOCK_WAIT_TIMEOUT:
            kind, key = "lock_timeout", "lock_timeouts"
        else:
            return None

        if rolled_back is True:
            steps = ["rolled back"]
        elif rolled_back is False:
            steps = ["rollback FAILED (connection discarded)"]
        else:
            steps = []
        steps.append("no retries left" if retry_in is None
                     else f"retry in {retry_in:.2f} s")
        message = " ".join(str(a) for a in getattr(exc, "args", ())[1:2])[:MAX_MESSAGE_LEN]

        with self._lock:
            self._counts[key] += 1
            if rolled_back is True:
                self._counts["rollbacks"] += 1
            elif rolled_back is False:
                self._counts["rollback_failures"] += 1
            entry = self._append_locked({
                "type": "error", "kind": kind, "code": code,
                "where": where or "", "attempt": attempt,
                "total_attempts": total_attempts,
                "action": "; ".join(steps), "message": message,
            })
            self._last = {"kind": kind, "code": code, "where": where or "",
                          "at": entry["at"]}
        return kind

    def record_retry(self):
        with self._lock:
            self._counts["retries"] += 1

    def record_recovered(self, where=None, attempts=None):
        with self._lock:
            self._counts["recovered"] += 1
            self._append_locked({
                "type": "recovered", "kind": "recovered", "code": None,
                "where": where or "", "attempt": attempts, "total_attempts": None,
                "action": (f"succeeded on attempt {attempts}" if attempts
                           else "succeeded after a retry"),
                "message": "",
            })

    def record_exhausted(self):
        with self._lock:
            self._counts["exhausted"] += 1

    def record_python_lock_timeout(self, where=None):
        with self._lock:
            self._counts["python_lock_timeouts"] += 1
            entry = self._append_locked({
                "type": "error", "kind": "python_lock_timeout", "code": None,
                "where": where or "", "attempt": None, "total_attempts": None,
                "action": "in-process lock wait timed out; task retried",
                "message": "",
            })
            self._last = {"kind": "python_lock_timeout", "code": None,
                          "where": where or "", "at": entry["at"]}

    def snapshot(self):
        """Copy of the counters and the event log (newest first)."""
        with self._lock:
            data = dict(self._counts)
            last = dict(self._last) if self._last else None
            events = [dict(e) for e in reversed(self._events)]
            since = self._since
        stamp = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
        if last:
            last["at_text"] = stamp(last["at"])
        for event in events:
            event["at_text"] = stamp(event["at"])
        data["last_event"] = last
        data["events"] = events
        data["since"] = since
        data["since_text"] = stamp(since)
        data["total_mysql_events"] = data["deadlocks"] + data["lock_timeouts"]
        return data


lock_events = LockEventCounters()


# ============================================================
# Bounded retry of a whole transaction
# ============================================================

def run_with_lock_retry(operation, rollback, *, max_retries=3, base_delay=0.1,
                        max_delay=2.0, jitter=0.25, sleep=time.sleep,
                        counters=None, where=""):
    """
    Run operation() (a complete transaction, commit included). If it raises
    MySQL 1213/1205: call rollback(), wait with backoff and run it again, up
    to max_retries extra times. Returns operation()'s result.

    A rollback() that itself fails is swallowed (the connection is probably
    dead; the caller opens a fresh one on the next attempt if it needs to).
    """
    counters = lock_events if counters is None else counters
    max_retries = max(0, min(int(max_retries), MAX_RETRIES_LIMIT))
    total_attempts = max_retries + 1
    hit = False
    for attempt in range(1, total_attempts + 1):
        try:
            result = operation()
        except Exception as exc:
            if not is_lock_error(exc):
                raise
            hit = True
            try:
                rollback()
                rolled_back = True
            except Exception:
                rolled_back = False
            final = attempt > max_retries
            delay = None if final else backoff_delay(
                attempt, base=base_delay, cap=max_delay, jitter=jitter)
            counters.record_error(exc, where=where, attempt=attempt,
                                  total_attempts=total_attempts,
                                  retry_in=delay, rolled_back=rolled_back)
            if final:
                counters.record_exhausted()
                raise
            counters.record_retry()
            sleep(delay)
            continue
        if hit:
            counters.record_recovered(where=where, attempts=attempt)
        return result
    raise RuntimeError("unreachable")  # pragma: no cover