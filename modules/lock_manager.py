"""
Recroot - Lock ordering and bounded lock acquisition

Pure Python: no Flask, no MySQL, no worker threads of its own.

Why this exists
    A deadlock needs a circular wait: thread 1 holds A and waits for B while
    thread 2 holds B and waits for A. The standard cure is a TOTAL ORDER on
    locks: if every thread acquires locks only in ascending rank, a cycle is
    impossible. Until now that rule was only a comment in modules/worker.py
    ("never held together"). RankedLock makes it enforceable:

      * every lock has a numeric rank;
      * a thread that already holds a lock may only block on a lock of
        STRICTLY HIGHER rank, otherwise LockOrderError is raised at once,
        before the thread can ever wait (so the mistake shows up in the first
        test run, not as a rare production hang);
      * try-locks (acquire(blocking=False)) are exempt: they cannot wait, so
        they cannot take part in a deadlock;
      * a lock may carry a default_timeout so a lock held across slow I/O can
        never block another thread forever (LockAcquireTimeout).

Ranks used by the real application (lower is acquired first)
    RANK_DEDUP       10   worker._dedup_lock      (held across DB I/O)
    RANK_STATE       20   worker._state_lock      (in-memory state only)
    RANK_SCHEDULER   30   scheduler queue lock    (in-memory only)

    The only nesting that exists today is state (20) -> scheduler (30) inside
    ResumeWorkerPool.get_stats(). It is ascending, so it is allowed.

Compatibility
    RankedLock works as the lock of a threading.Condition (it implements
    _is_owned / _release_save / _acquire_restore), which is how
    modules.scheduler.SchedulerQueue(lock=...) uses it. The scheduler module
    itself is not modified and its ordering logic is untouched.
"""

import threading
import time
from contextlib import contextmanager

__all__ = [
    "RANK_DEDUP", "RANK_STATE", "RANK_SCHEDULER",
    "LockOrderError", "LockAcquireTimeout",
    "RankedLock", "acquire_in_order", "held_lock_names",
]

RANK_DEDUP = 10
RANK_STATE = 20
RANK_SCHEDULER = 30


class LockOrderError(RuntimeError):
    """A thread tried to block on a lock that violates the global order
    (or tried to re-acquire a non-reentrant lock it already holds)."""


class LockAcquireTimeout(TimeoutError):
    """A lock could not be acquired within its time limit."""


# Locks currently held by THIS thread, in acquisition order.
_held = threading.local()


def _stack():
    stack = getattr(_held, "stack", None)
    if stack is None:
        stack = _held.stack = []
    return stack


def held_lock_names():
    """Names of the RankedLocks the calling thread holds (for diagnostics)."""
    return tuple(lock.name for lock in _stack())


class RankedLock:
    """Non-reentrant mutex with a rank. See the module docstring."""

    def __init__(self, name, rank, default_timeout=None):
        if default_timeout is not None and default_timeout <= 0:
            raise ValueError("default_timeout must be positive or None.")
        self.name = str(name)
        self.rank = int(rank)
        self.default_timeout = default_timeout
        self._lock = threading.Lock()

    def __repr__(self):
        state = "locked" if self._lock.locked() else "unlocked"
        return f"<RankedLock {self.name!r} rank={self.rank} {state}>"

    # --------------------------------------------------------
    # Order check
    # --------------------------------------------------------

    def _check_order(self):
        stack = _stack()
        if not stack:
            return
        for held in stack:
            if held is self:
                raise LockOrderError(
                    f"{self.name!r} is already held by this thread "
                    f"(the lock is not reentrant; re-acquiring would self-deadlock)."
                )
        highest = max(stack, key=lambda lock: lock.rank)
        if highest.rank >= self.rank:
            raise LockOrderError(
                f"Lock order violation: acquiring {self.name!r} (rank {self.rank}) "
                f"while holding {highest.name!r} (rank {highest.rank}). "
                f"Locks must be taken in ascending rank."
            )

    # --------------------------------------------------------
    # Lock protocol
    # --------------------------------------------------------

    def acquire(self, blocking=True, timeout=-1):
        """Same contract as threading.Lock.acquire, plus the order check for
        blocking acquisitions. Returns True/False; never raises on timeout."""
        if blocking:
            self._check_order()
            acquired = self._lock.acquire(True, timeout)
        else:
            acquired = self._lock.acquire(False)   # try-lock: cannot deadlock
        if acquired:
            _stack().append(self)
        return acquired

    def release(self):
        stack = _stack()
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] is self:
                del stack[index]
                break
        else:
            raise RuntimeError(
                f"release of {self.name!r}, which this thread does not hold."
            )
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        if self.default_timeout is None:
            self.acquire()
        elif not self.acquire(True, self.default_timeout):
            raise LockAcquireTimeout(
                f"Could not acquire {self.name!r} within {self.default_timeout:g}s."
            )
        return True

    def __exit__(self, exc_type, exc, tb):
        self.release()

    # --------------------------------------------------------
    # threading.Condition support
    # --------------------------------------------------------

    def _is_owned(self):
        return self in _stack()

    def _release_save(self):
        self.release()
        return None

    def _acquire_restore(self, _state):
        # Re-acquiring after Condition.wait(): the thread held nothing ranked
        # above this lock when it started waiting, so no order check is needed.
        self._lock.acquire()
        _stack().append(self)


@contextmanager
def acquire_in_order(locks, timeout=None):
    """
    Acquire several RankedLocks in ascending rank, whatever order the caller
    listed them in, and release them in reverse order on exit.

    timeout is the total budget in seconds (None waits indefinitely). If the
    budget runs out, everything acquired so far is released and
    LockAcquireTimeout is raised, so a failed attempt never leaves a lock held.
    Ranks within one call must be unique (a total order is required).
    """
    unique = []
    for lock in locks:
        if all(lock is not other for other in unique):
            unique.append(lock)
    ranks = [lock.rank for lock in unique]
    if len(set(ranks)) != len(ranks):
        raise ValueError("acquire_in_order needs distinct ranks for distinct locks.")
    ordered = sorted(unique, key=lambda lock: lock.rank)

    deadline = None if timeout is None else time.monotonic() + timeout
    acquired = []
    try:
        for lock in ordered:
            if deadline is None:
                lock.acquire()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not lock.acquire(True, remaining):
                    raise LockAcquireTimeout(
                        f"Could not acquire {lock.name!r} within {timeout:g}s."
                    )
            acquired.append(lock)
        yield tuple(ordered)
    finally:
        for lock in reversed(acquired):
            lock.release()
