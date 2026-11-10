"""
Recroot - Scheduler (FCFS + deadline-based priority)

Stage 1 of the FCFS / priority-scheduling work. This module is PURE PYTHON:
no Flask, no MySQL, no worker threads of its own. modules/worker.py will
use it in a later stage to decide which waiting application a free worker
takes next. Keeping it separate means the ordering rules can be unit-tested
completely on their own (tests/test_scheduler.py).

--------------------------------------------------------------------------
Two scheduling algorithms (selectable, independent of each other)
--------------------------------------------------------------------------

FCFS (first come, first served)
    Order key:  (applied_at, application_id)
    The application that was submitted earliest is selected first.
    applications.applied_at has only 1-second resolution, so the
    application id (AUTO_INCREMENT, i.e. arrival order) breaks ties.

Priority (deadline-based, earliest deadline first)
    Urgency comes from job_postings.deadline (a DATE, nullable). Every
    waiting application falls into exactly one bucket, evaluated against
    "today" at the moment of the scheduling decision:

        bucket 0  UPCOMING     deadline >= today   (a deadline of today is
                                                    still upcoming: a DATE
                                                    has no time of day)
        bucket 1  NO DEADLINE  deadline IS NULL
        bucket 2  EXPIRED      deadline <  today

    Order key:  (bucket, deadline, applied_at, application_id)
      * Upcoming:     earliest deadline first; equal deadlines are ordered
                      by applied_at, then application id.
      * No deadline:  no urgency information, so applied_at, then id (FCFS).
      * Expired:      applied_at, then id. (Not by deadline: the deadline
                      has passed, so how long ago it passed adds no urgency.)

    Expired applications are NEVER skipped or dropped. The candidate applied
    and the recruiter still needs the score; they simply wait behind
    everything that still has an upcoming deadline or no deadline.

    The bucket is computed when a task is SELECTED, not when it is queued,
    so an application that waits past midnight is classified correctly.

--------------------------------------------------------------------------
Selection order is not completion order
--------------------------------------------------------------------------
The scheduler controls the order in which tasks are SELECTED (handed to a
worker). With several worker threads running at once, tasks selected 1st
and 2nd start almost together and may FINISH in either order, because
parsing one PDF can take longer than parsing another. Every decision
returned by SchedulerQueue.pop_next() carries a dispatch sequence number
(selection order); completion order is recorded by the worker, so the two
can be shown side by side. With a single worker thread they are identical.

--------------------------------------------------------------------------
Modes
--------------------------------------------------------------------------
AUTOMATIC (default, matches how Recroot behaves today)
    A task becomes eligible as soon as it is added. A free worker takes the
    best eligible task under the selected algorithm. The algorithm only
    changes the order when there is a backlog (more waiting tasks than free
    workers); with idle workers a new task simply starts at once.

MANUAL (controlled demonstration)
    New tasks are HELD: they sit in the queue and workers ignore them. Only
    run_scheduler() releases the currently held batch, using the algorithm
    selected at that moment for the whole run. Tasks that arrive during a
    run stay held for the next run.

Thread safety
    One Condition (wrapping one lock) guards all state. add(), pop_next(),
    set_mode(), run_scheduler() and the rest are each atomic. pop_next()
    removes the chosen task inside that critical section, so two worker
    threads can never receive the same task. This module never touches the
    database and holds its lock only for short in-memory operations.
    (This is the in-process half of "workers cannot claim the same
    application"; the database claim is a separate step in worker.py.)
"""

import queue
import threading
import time
from collections import OrderedDict, namedtuple
from datetime import date, datetime

__all__ = [
    "ALGORITHM_FCFS", "ALGORITHM_PRIORITY", "ALGORITHMS", "ALGORITHM_LABELS",
    "MODE_AUTOMATIC", "MODE_MANUAL", "MODES",
    "BUCKET_UPCOMING", "BUCKET_NO_DEADLINE", "BUCKET_EXPIRED", "BUCKET_LABELS",
    "STATE_ELIGIBLE", "STATE_HELD",
    "ADD_QUEUED", "ADD_HELD", "ADD_FULL", "ADD_DUPLICATE", "ADD_CLOSED",
    "SchedulerError", "SchedulerStateError",
    "ScheduledTask", "DispatchDecision",
    "make_task", "normalize_deadline", "normalize_applied_at",
    "deadline_bucket", "fcfs_key", "priority_key", "sort_key",
    "order_tasks", "queue_positions",
    "MAX_DISPATCH_WINDOW_SECONDS", "normalize_dispatch_window",
    "SchedulerQueue",
]


# ============================================================
# Constants
# ============================================================

ALGORITHM_FCFS = "fcfs"
ALGORITHM_PRIORITY = "priority"
ALGORITHMS = (ALGORITHM_FCFS, ALGORITHM_PRIORITY)
ALGORITHM_LABELS = {
    ALGORITHM_FCFS: "FCFS (first come, first served)",
    ALGORITHM_PRIORITY: "Priority (earliest deadline first)",
}

MODE_AUTOMATIC = "automatic"
MODE_MANUAL = "manual"
MODES = (MODE_AUTOMATIC, MODE_MANUAL)

BUCKET_UPCOMING = 0
BUCKET_NO_DEADLINE = 1
BUCKET_EXPIRED = 2
BUCKET_LABELS = {
    BUCKET_UPCOMING: "upcoming",
    BUCKET_NO_DEADLINE: "no deadline",
    BUCKET_EXPIRED: "expired",
}

# Where a waiting task is sitting inside the queue
STATE_ELIGIBLE = "eligible"    # workers may take it
STATE_HELD = "held"            # manual mode: waiting for Run Scheduler

# Results of SchedulerQueue.add()
ADD_QUEUED = "queued"          # accepted, eligible now (automatic mode)
ADD_HELD = "held"              # accepted, held for Run Scheduler (manual mode)
ADD_FULL = "full"              # rejected: queue at capacity
ADD_DUPLICATE = "duplicate"    # rejected: that application is already waiting
ADD_CLOSED = "closed"          # rejected: queue has been closed


class SchedulerError(Exception):
    """Base class for scheduler errors."""


class SchedulerStateError(SchedulerError):
    """An action was refused because of the queue's current state
    (e.g. Run Scheduler while in automatic mode). The message is written
    to be shown to an administrator."""


# ============================================================
# Task records
# ============================================================

# One waiting application. Immutable, so it can be shared between threads
# and shown on a page without copying.
#   applied_at         naive datetime (applications.applied_at)
#   deadline           datetime.date or None (job_postings.deadline)
#   enqueued_at        time.time() when the worker accepted it
#   metadata_missing   True if applied_at could not be read from the database
#                      (the enqueue time was used instead)
ScheduledTask = namedtuple(
    "ScheduledTask",
    ["application_id", "applied_at", "deadline", "job_title",
     "enqueued_at", "metadata_missing"],
)

# What pop_next() returns: the task plus the facts of the decision.
#   dispatch_seq   1, 2, 3... order in which tasks were SELECTED
#   queue_size     eligible tasks competing at that moment (incl. the winner)
#   passed_over    how many of those had an EARLIER FCFS key than the winner
#                  (always 0 under FCFS; shows how far Priority jumped ahead)
#   bucket         the winner's deadline bucket on the decision date
DispatchDecision = namedtuple(
    "DispatchDecision",
    ["task", "algorithm", "mode", "run_id", "dispatch_seq",
     "queue_size", "passed_over", "bucket"],
)


def normalize_deadline(value):
    """
    Accept None, a date, a datetime, or an ISO 'YYYY-MM-DD' string and
    return a datetime.date (or None). pymysql returns DATE columns as
    datetime.date, so the conversions are only for safety and tests.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            raise ValueError(f"Invalid deadline: {value!r}") from None
    raise TypeError(f"Unsupported deadline type: {type(value).__name__}")


def normalize_applied_at(value):
    """Return a naive datetime, or None. (Naive values can always be
    compared with each other; mixing naive and aware raises TypeError.)"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip()).replace(tzinfo=None)
        except ValueError:
            raise ValueError(f"Invalid applied_at: {value!r}") from None
    raise TypeError(f"Unsupported applied_at type: {type(value).__name__}")


def make_task(application_id, applied_at=None, deadline=None, job_title=None,
              enqueued_at=None, metadata_missing=False):
    """
    Build a ScheduledTask, normalising the values.

    If applied_at is None (the database lookup failed) the enqueue time is
    used and the task is flagged metadata_missing, so it is still scheduled
    and the admin page can say why its arrival time is approximate.
    A missing deadline is NOT an error: it means "no deadline".
    """
    enqueued = float(enqueued_at) if enqueued_at is not None else time.time()
    applied = normalize_applied_at(applied_at)
    if applied is None:
        applied = datetime.fromtimestamp(enqueued)
        metadata_missing = True
    return ScheduledTask(
        application_id=int(application_id),
        applied_at=applied,
        deadline=normalize_deadline(deadline),
        job_title=job_title or "",
        enqueued_at=enqueued,
        metadata_missing=bool(metadata_missing),
    )


# ============================================================
# Ordering rules (pure functions)
# ============================================================

def _check_algorithm(algorithm):
    if algorithm not in ALGORITHMS:
        raise ValueError(
            f"Unknown scheduling algorithm {algorithm!r}; "
            f"expected one of {ALGORITHMS}."
        )


def _today_or_default(today):
    return date.today() if today is None else normalize_deadline(today)


def deadline_bucket(deadline, today=None):
    """BUCKET_UPCOMING, BUCKET_NO_DEADLINE or BUCKET_EXPIRED."""
    deadline = normalize_deadline(deadline)
    if deadline is None:
        return BUCKET_NO_DEADLINE
    today = _today_or_default(today)
    return BUCKET_UPCOMING if deadline >= today else BUCKET_EXPIRED


def fcfs_key(task):
    """FCFS: earliest applied_at first, application id breaks ties."""
    return (task.applied_at, task.application_id)


def priority_key(task, today=None):
    """
    Deadline priority. Smaller key = selected earlier.
    The deadline only matters inside the UPCOMING bucket; for the other two
    buckets that slot is a constant 0, so they order by applied_at then id.
    """
    today = _today_or_default(today)
    bucket = deadline_bucket(task.deadline, today)
    ordinal = task.deadline.toordinal() if bucket == BUCKET_UPCOMING else 0
    return (bucket, ordinal, task.applied_at, task.application_id)


def sort_key(algorithm, task, today=None):
    """The single place that maps an algorithm name to its ordering key."""
    _check_algorithm(algorithm)
    if algorithm == ALGORITHM_FCFS:
        return fcfs_key(task)
    return priority_key(task, today)


def order_tasks(tasks, algorithm, today=None):
    """Tasks sorted into the order the algorithm would select them."""
    _check_algorithm(algorithm)
    today = _today_or_default(today)
    return sorted(tasks, key=lambda t: sort_key(algorithm, t, today))


def queue_positions(tasks, algorithm, today=None):
    """[(position, task), ...] with positions starting at 1."""
    return [(i, t) for i, t in enumerate(order_tasks(tasks, algorithm, today), 1)]


# ============================================================
# The scheduler-owned queue
# ============================================================

MAX_DISPATCH_WINDOW_SECONDS = 60.0


def normalize_dispatch_window(value):
    """
    Validate the optional dispatch window (seconds). 0 means disabled.
    Accepts int/float or a numeric string (an environment variable); rejects
    negative, non-finite and absurdly long values (> 60 s), because the window
    is a demo aid and a typo must not silently stall every application.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise ValueError("dispatch window must be a number of seconds, not a boolean.")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"dispatch window must be a number of seconds, got {value!r}.") from None
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise ValueError("dispatch window must be a finite number of seconds.")
    if seconds < 0:
        raise ValueError("dispatch window cannot be negative.")
    if seconds > MAX_DISPATCH_WINDOW_SECONDS:
        raise ValueError(
            f"dispatch window cannot exceed {MAX_DISPATCH_WINDOW_SECONDS:g} seconds.")
    return seconds


class SchedulerQueue:
    """
    Thread-safe collection of waiting tasks that hands them to worker threads
    in scheduling order. It replaces "a FIFO queue.Queue" as the thing that
    decides order; it does not run any threads itself.

    Producers call add(task). Worker threads call pop_next(), which blocks
    until a task is eligible and returns a DispatchDecision (or None once the
    queue is closed). The admin page reads snapshot().
    """

    def __init__(self, capacity=100, algorithm=ALGORITHM_FCFS,
                 mode=MODE_AUTOMATIC, today_func=None, lock=None,
                 dispatch_window_seconds=0.0):
        """
        capacity    maximum waiting tasks (eligible + held), like the
                    bounded queue it replaces.
        today_func  returns "today" as a date; injectable so tests can fix
                    the date. Must be cheap: it is called inside the lock.
        lock        optional lock object to wrap (a later stage passes a
                    ranked lock from lock_manager); defaults to a plain Lock.
        dispatch_window_seconds
                    DEMO AID, disabled (0) by default. When > 0, the moment a
                    task arrives in an EMPTY queue a window of this length
                    opens; workers may not select anything until it closes,
                    so tasks arriving meanwhile can be compared and the
                    algorithm's order becomes visible. When it closes, every
                    waiting task is a candidate and the selection rules are
                    exactly the normal ones. The window is fixed (later
                    arrivals do not extend it), applies to both algorithms,
                    and is not used for manual-mode runs.
        """
        if int(capacity) < 1:
            raise ValueError("capacity must be at least 1 (the queue is bounded).")
        _check_algorithm(algorithm)
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode!r}; expected one of {MODES}.")

        self._capacity = int(capacity)
        self._algorithm = algorithm
        self._mode = mode
        self._today_func = today_func or date.today
        self._cond = threading.Condition(lock if lock is not None else threading.Lock())
        self._window_seconds = normalize_dispatch_window(dispatch_window_seconds)
        self._window_until = 0.0         # time.monotonic() when the open window closes

        self._eligible = OrderedDict()   # application_id -> task (workers may take)
        self._held = OrderedDict()       # application_id -> task (manual mode)
        self._closed = False
        self._dispatch_seq = 0           # selections made so far
        self._run_counter = 0
        self._run = None                 # info about the most recent manual run

    # --------------------------------------------------------
    # Settings
    # --------------------------------------------------------

    def get_algorithm(self):
        with self._cond:
            return self._algorithm

    def set_algorithm(self, algorithm):
        """
        Change the algorithm used for FUTURE selections. A manual run that is
        already in progress keeps the algorithm it started with. Returns the
        previous algorithm.
        """
        _check_algorithm(algorithm)
        with self._cond:
            previous, self._algorithm = self._algorithm, algorithm
            return previous

    def get_mode(self):
        with self._cond:
            return self._mode

    def set_mode(self, mode):
        """
        Switch between automatic and manual mode. Returns the number of
        waiting tasks that changed state.

        manual -> automatic: every held task becomes eligible immediately.
        automatic -> manual: every task not yet taken by a worker becomes
            held. Tasks a worker already took are not affected.
        """
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode!r}; expected one of {MODES}.")
        with self._cond:
            if mode == self._mode:
                return 0
            self._mode = mode
            self._run = None
            if mode == MODE_AUTOMATIC:
                moved = len(self._held)
                self._eligible.update(self._held)
                self._held.clear()
                self._cond.notify_all()
            else:
                moved = len(self._eligible)
                self._held.update(self._eligible)
                self._eligible.clear()
            return moved

    # --------------------------------------------------------
    # Producer side
    # --------------------------------------------------------

    def add(self, task):
        """
        Add one waiting task. Never blocks. Returns one of ADD_QUEUED,
        ADD_HELD, ADD_FULL, ADD_DUPLICATE, ADD_CLOSED.
        """
        with self._cond:
            if self._closed:
                return ADD_CLOSED
            app_id = task.application_id
            if app_id in self._eligible or app_id in self._held:
                return ADD_DUPLICATE
            if len(self._eligible) + len(self._held) >= self._capacity:
                return ADD_FULL
            if self._mode == MODE_MANUAL:
                self._held[app_id] = task
                return ADD_HELD
            if self._window_seconds > 0 and not self._eligible:
                self._window_until = time.monotonic() + self._window_seconds
            self._eligible[app_id] = task
            self._cond.notify()
            return ADD_QUEUED

    def run_scheduler(self):
        """
        Manual mode only: release every currently held task as one batch,
        to be selected using the algorithm selected right now (for the whole
        run, even if the setting is changed afterwards).

        Raises SchedulerStateError (message is admin-readable) if not in
        manual mode, if nothing is held, or if the previous run still has
        tasks no worker has taken yet. Returns a dict describing the run:
        run_id, algorithm, size, planned_order (application ids), started_at.
        """
        with self._cond:
            if self._mode != MODE_MANUAL:
                raise SchedulerStateError(
                    "Run Scheduler is only available in manual mode.")
            if self._eligible:
                raise SchedulerStateError(
                    f"The previous run still has {len(self._eligible)} "
                    "application(s) waiting for a worker.")
            if not self._held:
                raise SchedulerStateError("No held applications to schedule.")

            today = self._today_func()
            algorithm = self._algorithm
            batch = order_tasks(self._held.values(), algorithm, today)

            self._run_counter += 1
            self._run = {
                "run_id": self._run_counter,
                "algorithm": algorithm,
                "size": len(batch),
                "planned_order": [t.application_id for t in batch],
                "started_at": time.time(),
            }
            self._window_until = 0.0     # an admin-started run is never delayed
            self._eligible.update((t.application_id, t) for t in batch)
            self._held.clear()
            self._cond.notify_all()
            return self._copy_run()

    # --------------------------------------------------------
    # Consumer side
    # --------------------------------------------------------

    def pop_next(self, timeout=None):
        """
        Block until a task is eligible, remove the best one under the active
        algorithm, and return its DispatchDecision.

        Returns None if the queue is closed. Raises queue.Empty if `timeout`
        seconds pass with nothing eligible (timeout=None waits forever).
        The removal happens inside the lock, so each task is returned to
        exactly one caller.
        """
        end = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while True:
                if self._closed:
                    return None
                window_left = self._window_remaining()
                if self._eligible and window_left <= 0:
                    break
                # Wait for a task to arrive, or for the dispatch window to
                # close (whichever is relevant), without outliving `timeout`.
                sleep = window_left if self._eligible else None
                if end is not None:
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        raise queue.Empty
                    sleep = remaining if sleep is None else min(sleep, remaining)
                self._cond.wait(sleep)

            today = self._today_func()
            algorithm = self._active_algorithm()
            candidates = list(self._eligible.values())
            chosen = min(candidates, key=lambda t: sort_key(algorithm, t, today))
            del self._eligible[chosen.application_id]

            chosen_fcfs = fcfs_key(chosen)
            passed_over = sum(1 for t in candidates if fcfs_key(t) < chosen_fcfs)
            self._dispatch_seq += 1
            return DispatchDecision(
                task=chosen,
                algorithm=algorithm,
                mode=self._mode,
                run_id=self._run["run_id"] if (self._mode == MODE_MANUAL and self._run) else None,
                dispatch_seq=self._dispatch_seq,
                queue_size=len(candidates),
                passed_over=passed_over,
                bucket=deadline_bucket(chosen.deadline, today),
            )

    # --------------------------------------------------------
    # Inspection (used by the admin page and by tests)
    # --------------------------------------------------------

    def snapshot(self, today=None):
        """
        What is waiting, in the order the scheduler would select it.

        Eligible tasks are ranked under the algorithm that is actually in
        force. Held tasks (manual mode) are ranked under the currently
        selected algorithm and are only a PLAN: they are not selected until
        Run Scheduler is clicked. The positions come from the same key
        function pop_next() uses, so the display is the real order.
        """
        with self._cond:
            today = _today_or_default(today) if today is not None else self._today_func()
            rows = []
            groups = (
                (STATE_ELIGIBLE, list(self._eligible.values()), self._active_algorithm()),
                (STATE_HELD, list(self._held.values()), self._algorithm),
            )
            for state, tasks, algorithm in groups:
                for position, task in queue_positions(tasks, algorithm, today):
                    bucket = deadline_bucket(task.deadline, today)
                    rows.append({
                        "application_id": task.application_id,
                        "job_title": task.job_title,
                        "deadline": task.deadline,
                        "bucket": bucket,
                        "bucket_label": BUCKET_LABELS[bucket],
                        "applied_at": task.applied_at,
                        "enqueued_at": task.enqueued_at,
                        "metadata_missing": task.metadata_missing,
                        "state": state,
                        "position": position,
                        "algorithm": algorithm,
                    })
            return {
                "algorithm": self._algorithm,
                "mode": self._mode,
                "run": self._copy_run(),
                "today": today,
                "capacity": self._capacity,
                "dispatch_window_seconds": self._window_seconds,
                "dispatch_window_remaining": self._window_remaining() if self._eligible else 0.0,
                "eligible_count": len(self._eligible),
                "held_count": len(self._held),
                "dispatched_total": self._dispatch_seq,
                "closed": self._closed,
                "tasks": rows,
            }

    def get_dispatch_window(self):
        """Configured dispatch window in seconds (0 = disabled)."""
        return self._window_seconds

    def pending_count(self):
        """Waiting tasks (eligible + held)."""
        with self._cond:
            return len(self._eligible) + len(self._held)

    def contains(self, application_id):
        with self._cond:
            return application_id in self._eligible or application_id in self._held

    # --------------------------------------------------------
    # Shutdown
    # --------------------------------------------------------

    def discard_all(self):
        """Remove every waiting task without running it; returns them."""
        with self._cond:
            removed = list(self._eligible.values()) + list(self._held.values())
            self._eligible.clear()
            self._held.clear()
            return removed

    def close(self):
        """Refuse new tasks and wake every blocked pop_next(), which then
        returns None."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # --------------------------------------------------------
    # Internals (call with the lock held)
    # --------------------------------------------------------

    def _window_remaining(self):
        """Seconds until the open dispatch window closes (0 if none is open)."""
        if self._window_seconds <= 0:
            return 0.0
        return max(0.0, self._window_until - time.monotonic())

    def _active_algorithm(self):
        """The algorithm in force for selections right now: a manual run
        keeps the algorithm it was started with."""
        if self._mode == MODE_MANUAL and self._run is not None:
            return self._run["algorithm"]
        return self._algorithm

    def _copy_run(self):
        if self._run is None:
            return None
        run = dict(self._run)
        run["planned_order"] = list(self._run["planned_order"])
        return run