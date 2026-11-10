"""
Recroot - Deadlock demonstration (SIMULATION ONLY)

Two small experiments for the admin "Deadlock Demo" page.

SAFETY / ISOLATION
    * Every run creates its OWN brand-new locks. They are never shared with the
      worker pool, the scheduler, Flask, or anything else in the application.
    * No database connection is opened and no SQL is executed. In particular
      nothing here can cause, or be mistaken for, a MySQL deadlock (error
      1213) in the live `recroot` database.
    * It does not touch modules.db_retry.lock_events, so running a demo never
      changes the "real MySQL lock events" shown on the page.
    * It cannot hang: every lock acquisition and every barrier has a timeout
      (clamped to MAX_TIMEOUT seconds), threads are daemons and are joined
      with a deadline, and all locks are released in `finally` blocks.
    * Only one demo runs at a time (a second request gets outcome "busy").

Every result dict carries simulated=True and database_involved=False so the
page and the tests can state unambiguously what it is.
"""

import threading
import time

from modules.lock_manager import (
    LockAcquireTimeout,
    LockOrderError,
    RankedLock,
    acquire_in_order,
)

__all__ = ["simulate_deadlock", "run_prevention_demo",
           "DEFAULT_TIMEOUT", "MIN_TIMEOUT", "MAX_TIMEOUT"]

DEFAULT_TIMEOUT = 1.5
MIN_TIMEOUT = 0.2
MAX_TIMEOUT = 5.0

_run_gate = threading.Lock()     # one demo at a time (try-lock only)


def _clamp(timeout):
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    if value != value:               # NaN
        value = DEFAULT_TIMEOUT
    return min(MAX_TIMEOUT, max(MIN_TIMEOUT, value))


class _Timeline:
    """Thread-safe, time-stamped list of what happened."""

    def __init__(self):
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._events = []

    def add(self, actor, message):
        entry = {"t": round(time.monotonic() - self._start, 3),
                 "actor": actor, "message": message}
        with self._lock:
            self._events.append(entry)

    def events(self):
        with self._lock:
            return sorted(self._events, key=lambda e: e["t"])

    def elapsed(self):
        return round(time.monotonic() - self._start, 3)


def _base_result(kind, title, timeout):
    return {
        "kind": kind,
        "title": title,
        "simulated": True,
        "database_involved": False,
        "timeout_seconds": timeout,
        "outcome": "error",
        "summary": "",
        "events": [],
        "elapsed_seconds": 0.0,
        "cleaned_up": True,
    }


def _busy(kind, title):
    result = _base_result(kind, title, 0)
    result["outcome"] = "busy"
    result["summary"] = "Another demo is already running. Try again in a moment."
    return result


def _join_all(threads, limit):
    """Join with an overall deadline. Returns True if every thread finished."""
    end = time.monotonic() + limit
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    return not any(thread.is_alive() for thread in threads)


# ============================================================
# 1. Simulate Deadlock: circular wait, detected by timeout
# ============================================================

def simulate_deadlock(timeout=DEFAULT_TIMEOUT):
    title = "Simulated deadlock (circular wait)"
    if not _run_gate.acquire(blocking=False):
        return _busy("simulated_deadlock", title)
    try:
        return _simulate_deadlock(_clamp(timeout), title)
    finally:
        _run_gate.release()


def _simulate_deadlock(timeout, title):
    result = _base_result("simulated_deadlock", title, timeout)
    timeline = _Timeline()
    lock_a, lock_b = threading.Lock(), threading.Lock()   # isolated, plain locks
    guard = timeout + 2.0
    both_holding = threading.Barrier(2, timeout=guard)
    both_attempted = threading.Barrier(2, timeout=guard)
    outcomes = {}

    def contender(actor, first, first_name, second, second_name):
        held = []
        try:
            if not first.acquire(timeout=timeout):
                outcomes[actor] = "error"
                timeline.add(actor, f"could not even take {first_name}")
                return
            held.append((first, first_name))
            timeline.add(actor, f"acquired lock {first_name}")
            both_holding.wait()           # make the overlap deterministic
            timeline.add(actor, f"now waiting for lock {second_name}, which the "
                                f"other thread holds (gives up after {timeout:g}s)")
            if second.acquire(timeout=timeout):
                held.append((second, second_name))
                outcomes[actor] = "acquired_both"
                timeline.add(actor, f"acquired lock {second_name} (no deadlock this time)")
            else:
                outcomes[actor] = "timed_out"
                timeline.add(actor, f"TIMED OUT waiting for lock {second_name}")
            # Keep the first lock until the other thread has also finished its
            # attempt, so one side cannot slip through just because the other
            # released early. This is what makes the outcome deterministic.
            both_attempted.wait()
        except threading.BrokenBarrierError:
            outcomes.setdefault(actor, "error")
            timeline.add(actor, "stopped: the other thread did not reach the meeting point")
        finally:
            for lock, name in reversed(held):
                lock.release()
                timeline.add(actor, f"released lock {name}")

    threads = [
        threading.Thread(target=contender, name="demo-deadlock-1", daemon=True,
                         args=("Thread 1", lock_a, "A", lock_b, "B")),
        threading.Thread(target=contender, name="demo-deadlock-2", daemon=True,
                         args=("Thread 2", lock_b, "B", lock_a, "A")),
    ]
    for thread in threads:
        thread.start()
    finished = _join_all(threads, guard + 2.0)

    result["events"] = timeline.events()
    result["elapsed_seconds"] = timeline.elapsed()
    result["cleaned_up"] = finished and not lock_a.locked() and not lock_b.locked()

    if all(outcomes.get(n) == "timed_out" for n in ("Thread 1", "Thread 2")):
        result["outcome"] = "deadlock_detected"
        result["summary"] = (
            "Circular wait reproduced: Thread 1 held A and waited for B while "
            "Thread 2 held B and waited for A. Neither could ever proceed; the "
            f"timeout ({timeout:g}s) is the only reason this demo ended. "
            "This used isolated in-memory locks only; no database was involved."
        )
    elif not finished:
        result["outcome"] = "error"
        result["summary"] = "A demo thread did not finish in time (this should not happen)."
    else:
        result["outcome"] = "no_deadlock"
        result["summary"] = ("The threads happened not to block each other this time: "
                             + ", ".join(f"{k}: {v}" for k, v in sorted(outcomes.items())) + ".")
    return result


# ============================================================
# 2. Run Prevention Demo: consistent lock ordering
# ============================================================

def run_prevention_demo(timeout=DEFAULT_TIMEOUT):
    title = "Prevention demo (consistent lock ordering)"
    if not _run_gate.acquire(blocking=False):
        return _busy("prevention_demo", title)
    try:
        return _run_prevention_demo(_clamp(timeout), title)
    finally:
        _run_gate.release()


def _run_prevention_demo(timeout, title):
    result = _base_result("prevention_demo", title, timeout)
    timeline = _Timeline()
    lock_a = RankedLock("A", rank=1)      # isolated; NOT the application's locks
    lock_b = RankedLock("B", rank=2)
    work = min(0.15, timeout / 4)
    t1_has_a = threading.Event()
    outcomes = {}
    flags = {"violation_detected": False}

    def thread_one():
        actor = "Thread 1"
        try:
            with acquire_in_order([lock_a, lock_b], timeout=timeout):
                t1_has_a.set()
                timeline.add(actor, "holds A then B (rank order: A=1, B=2); working")
                time.sleep(work)
            timeline.add(actor, "released B then A")
            outcomes[actor] = "completed"
        except LockAcquireTimeout:
            outcomes[actor] = "timed_out"
            timeline.add(actor, "TIMED OUT")
        finally:
            t1_has_a.set()       # never leave the other thread waiting on us

    def thread_two():
        actor = "Thread 2"
        try:
            t1_has_a.wait(timeout)
            # (a) The dangerous order, B then A, is refused BEFORE it can wait.
            if lock_b.acquire(True, timeout):
                try:
                    timeline.add(actor, "took B first and now tries A (the wrong order)")
                    lock_a.acquire(True, timeout)
                except LockOrderError as err:
                    flags["violation_detected"] = True
                    timeline.add(actor, f"REFUSED without waiting: {err}")
                else:  # pragma: no cover - would mean the guard is broken
                    lock_a.release()
                    timeline.add(actor, "wrong order was NOT refused (guard failure)")
                finally:
                    lock_b.release()
                    timeline.add(actor, "released B")
            # (b) The same two locks requested through the ordering helper.
            with acquire_in_order([lock_b, lock_a], timeout=timeout):
                timeline.add(actor, "asked for B and A; helper acquired A then B "
                                    "(waited for Thread 1 to finish, no cycle possible)")
                time.sleep(work)
            timeline.add(actor, "released B then A")
            outcomes[actor] = "completed"
        except LockAcquireTimeout:
            outcomes[actor] = "timed_out"
            timeline.add(actor, "TIMED OUT")

    threads = [
        threading.Thread(target=thread_one, name="demo-prevent-1", daemon=True),
        threading.Thread(target=thread_two, name="demo-prevent-2", daemon=True),
    ]
    for thread in threads:
        thread.start()
    finished = _join_all(threads, timeout * 3 + 3.0)

    result["events"] = timeline.events()
    result["elapsed_seconds"] = timeline.elapsed()
    result["cleaned_up"] = finished and not lock_a.locked() and not lock_b.locked()
    result["order_violation_detected"] = flags["violation_detected"]

    if (finished and outcomes.get("Thread 1") == "completed"
            and outcomes.get("Thread 2") == "completed"):
        result["outcome"] = "completed_without_deadlock"
        result["summary"] = (
            "Both threads needed A and B and both finished. Because every thread "
            "takes locks in the same order (A before B), a circular wait cannot "
            "form. A request in the wrong order was rejected immediately instead "
            "of blocking. In-memory locks only; no database was involved."
        )
    else:
        result["outcome"] = "error"
        result["summary"] = ("The prevention demo did not complete: "
                             + (", ".join(f"{k}: {v}" for k, v in sorted(outcomes.items()))
                                or "no thread reported") + ".")
    return result
