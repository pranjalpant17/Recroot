"""
Recroot — Background Worker / Processing Pipeline
Step 33 of the Phase 2 build.

Moves the heavy per-application work (Section 13 of the blueprint) out of
the request thread and into a fixed pool of worker threads:

    Flask request thread                      Worker threads (pool)
    --------------------                      ---------------------
    INSERT application, commit
    enqueue_application(id)  --> Queue --->   load application / job / user
    return immediately       (bounded, FIFO)  parse_resume()            (once)
                                              duplicate detection       (mutex)
                                              consistency analysis
                                              scoring engine
                                              one transaction: scores +
                                              duplicate + consistency +
                                              processing status + audit

Schema dependency (Decision A, approved)
    This module reads/writes three columns that do NOT exist in schema.sql
    yet and will be added in a later step:

        applications.processing_status  ENUM('queued','processing','done','failed')
        applications.processing_note    VARCHAR(500) NULL
        applications.processed_at       TIMESTAMP NULL

    Until schema.sql has them, the pipeline's UPDATEs will fail. Nothing
    imports this module yet, so the rest of the app is unaffected.

Scope / design rules
    * One task = one application_id. The worker re-reads everything from the
      database, so the queue holds no stale data and re-processing is safe.
    * Processing is IDEMPOTENT: index_resume() is a no-op if a fingerprint
      exists and scores are simply overwritten. That makes retries and the
      startup recovery sweep safe.
    * The worker writes ONLY the columns it owns (scores, duplicate fields,
      consistency fields, processing_*). It never touches `status`, so a
      recruiter's shortlist/reject decision survives re-processing.
    * No score penalties for duplicate/consistency flags (Sections 6, 7, 18);
      the stages are independent and only report information.
    * A failing stage never blocks the others. Results of stages that
      succeeded are still saved; a stage that failed leaves its old columns
      untouched (it does not overwrite a previously valid value with a default).
    * No Flask app context is used inside threads. init_worker(app) copies the
      needed settings once. Each thread owns its own MySQL connection.
    * Single-process design: the queue lives in memory. Tasks lost on a
      restart are recovered by recover_unprocessed() from the DB markers.
      (With several server processes each would have its own queue and its
      own recovery sweep - safe because processing is idempotent, but
      recover_unprocessed() could then re-run another process's in-progress
      work. Not an issue for the single-process deployment used here.)

Scheduling (FCFS / deadline priority)
    The bounded queue.Queue was replaced by modules.scheduler.SchedulerQueue.
    Workers still take one task each, but WHICH waiting application a free
    worker takes is decided by the selected algorithm:
        fcfs      applied_at, then application id
        priority  upcoming deadline (earliest first, today counts), then no
                  deadline, then expired; ties by applied_at, then id
    The algorithm can be switched at run time (set_scheduling_algorithm) and
    applies to every later selection. The order only matters when there is a
    backlog; an idle worker starts a new task at once. With several workers
    the order tasks are STARTED (dispatch_seq) can differ from the order
    they FINISH (completion_seq). No database schema change is involved.

    Optional DEMO dispatch window (WORKER_DISPATCH_WINDOW_SECONDS, default 0 =
    off). A worker normally starts a task the instant it arrives, so with an
    idle pool there is never a second task to compare it with. With a window
    of N seconds, the first task to arrive in an empty queue opens a window;
    nothing is selected until it closes, so applications submitted during it
    are ordered by the active algorithm. Cost: up to N seconds of added
    latency for every application, so leave it off in normal operation.

Typical use (wiring is done later in app.py / candidate.py)

    from modules.worker import init_worker, enqueue_application

    init_worker(app)                    # once, in create_app()
    ...
    db.commit()                         # application row is saved first
    enqueue_application(application_id) # then hand it to the pool
"""

import logging
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

from modules.consistency_checker import (
    application_consistency_fields,
    check_consistency,
)
from modules.db_retry import (
    backoff_delay,
    is_lock_error,
    lock_events,
    run_with_lock_retry,
)
from modules.duplicate_detector import (
    application_duplicate_fields,
    check_duplicates,
    index_resume,
)
from modules.lock_manager import (
    RANK_DEDUP,
    RANK_SCHEDULER,
    RANK_STATE,
    LockAcquireTimeout,
    RankedLock,
)
from modules.scheduler import (
    ADD_CLOSED,
    ADD_QUEUED,
    ALGORITHM_FCFS,
    BUCKET_LABELS,
    SchedulerQueue,
    make_task,
)
from modules.scoring_engine import save_score, score_application
from services.resume_parser import ResumeParseError, parse_resume

logger = logging.getLogger("recroot.worker")

__all__ = [
    "ResumeWorkerPool",
    "init_worker",
    "get_worker",
    "enqueue_application",
    "recover_unprocessed",
    "get_worker_stats",
    "get_task_status",
    "shutdown_worker",
    "get_scheduler_snapshot",
    "get_scheduling_algorithm",
    "set_scheduling_algorithm",
]


# ============================================================
# Constants
# ============================================================

DEFAULT_POOL_SIZE = 4
DEFAULT_QUEUE_SIZE = 100
DEFAULT_MAX_RETRIES = 2            # retries after the first attempt
DEFAULT_RETRY_BACKOFF = 0.5        # seconds; doubles each retry
MAX_TRACKED_TASKS = 500            # in-memory status registry size
NOTE_MAX_LEN = 500                 # applications.processing_note is VARCHAR(500)

# Deadlock handling (see modules/db_retry.py and modules/lock_manager.py)
DEFAULT_LOCK_WAIT_TIMEOUT = 10     # seconds; MySQL's own default is 50. Worker
                                   # connections only; None keeps the server default.
DEFAULT_DEDUP_LOCK_TIMEOUT = 120.0 # seconds a worker may wait for _dedup_lock
MAX_RETRY_DELAY = 8.0              # cap for the lock-error backoff, seconds

# processing_status values stored in the database
DB_QUEUED = "queued"
DB_PROCESSING = "processing"
DB_DONE = "done"
DB_FAILED = "failed"

# States kept in the in-memory registry (superset of the DB values)
STATE_QUEUED = "queued"
STATE_PROCESSING = "processing"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"          # application no longer exists
STATE_DISCARDED = "discarded"      # dropped from the queue at shutdown

_FINISHED_STATES = {STATE_DONE, STATE_FAILED, STATE_SKIPPED, STATE_DISCARDED}

# MySQL / connector error codes worth retrying: lock wait timeout, deadlock,
# too many connections, and lost/unavailable server connections.
# Everything else (bad SQL, unknown column, access denied) is NOT retried.
_TRANSIENT_MYSQL_CODES = {1040, 1205, 1213, 2002, 2003, 2006, 2013, 2055}



def _is_transient_db_error(exc):
    """True for database errors that may succeed if tried again."""
    try:
        import pymysql.err as mysql_err
    except ImportError:
        return False
    if isinstance(exc, mysql_err.InterfaceError):
        return True  # connection closed / not connected
    if isinstance(exc, mysql_err.OperationalError):
        code = exc.args[0] if exc.args else None
        return code in _TRANSIENT_MYSQL_CODES
    return False


def _truncate(text, limit=NOTE_MAX_LEN):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ============================================================
# The worker pool
# ============================================================

class ResumeWorkerPool:
    """
    Fixed-size thread pool fed by a bounded scheduler queue (FCFS or priority).

    Synchronisation objects. They are RankedLocks (modules/lock_manager.py):
    a thread may only block on a lock of HIGHER rank than any it already holds,
    and a violation raises LockOrderError instead of becoming a rare hang.
        _dedup_lock  rank 10. Serialises "fingerprint this resume, then compare
                     it with the others" so concurrent uploads see each other in
                     a deterministic order. The only lock held across DB I/O, so
                     waiting for it is time-limited (LockAcquireTimeout, retried
                     like a transient error). Parsing, consistency and scoring
                     run outside it.
        _state_lock  rank 20. Short critical sections over the shared in-memory
                     state: in-flight set, status registry, counters, and the
                     check-then-put in enqueue(). Never held during DB I/O.
        scheduler    rank 30. The SchedulerQueue's own lock (passed in through
                     its existing lock= parameter; scheduling logic unchanged).
    The one nesting that exists, _state_lock -> scheduler lock in get_stats(),
    is ascending. Database lock order: every transaction here writes the
    applications row first and audit_log last.
    """

    def __init__(self, db_config, upload_folder,
                 pool_size=DEFAULT_POOL_SIZE,
                 queue_max_size=DEFAULT_QUEUE_SIZE,
                 near_duplicate_threshold=85,
                 max_retries=DEFAULT_MAX_RETRIES,
                 retry_backoff=DEFAULT_RETRY_BACKOFF,
                 weights=None,
                 scheduling_algorithm=ALGORITHM_FCFS,
                 dispatch_window_seconds=0.0,
                 lock_wait_timeout=DEFAULT_LOCK_WAIT_TIMEOUT,
                 dedup_lock_timeout=DEFAULT_DEDUP_LOCK_TIMEOUT):
        if pool_size < 1:
            raise ValueError("pool_size must be at least 1.")
        if queue_max_size < 1:
            raise ValueError("queue_max_size must be at least 1 (the queue is bounded).")

        self._db_config = dict(db_config)
        self._upload_folder = upload_folder
        self._pool_size = int(pool_size)
        self._near_duplicate_threshold = int(near_duplicate_threshold)
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff = float(retry_backoff)
        self._weights = weights  # None -> scoring engine's default weights
        # innodb_lock_wait_timeout for THIS pool's connections (None = server default)
        self._lock_wait_timeout = (None if lock_wait_timeout in (None, 0)
                                   else max(1, min(3600, int(lock_wait_timeout))))
    #queue (scheduler decides the order tasks are handed to workers)
        self._scheduler = SchedulerQueue(capacity=int(queue_max_size),
                                         algorithm=scheduling_algorithm,
                                         dispatch_window_seconds=dispatch_window_seconds,
                                         lock=RankedLock("scheduler.queue", RANK_SCHEDULER))
        self._queue_capacity = int(queue_max_size)

    #synchronization
        self._state_lock = RankedLock("worker.state", RANK_STATE)
        self._dedup_lock = RankedLock("worker.dedup", RANK_DEDUP,
                                      default_timeout=dedup_lock_timeout)
        self._local = threading.local()      # per-thread DB connection

        self._threads = []
        self._accepting = False
        self._started_at = None
        self._last_recovery = None           # result of the latest recovery sweep

        # --- shared state, guarded by _state_lock ---
        self._in_flight = set()              # application ids queued or running
        self._tasks = OrderedDict()          # application_id -> status dict
        self._active = 0                     # workers currently processing
        self._completion_seq = 0             # order in which tasks FINISHED
        self._stats = {
            "enqueued": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "retries": 0,
            "rejected_full": 0,
            "rejected_in_flight": 0,
            "total_seconds": 0.0,
            "max_seconds": 0.0,
        }

    # --------------------------------------------------------
# THREAD 
    # --------------------------------------------------------

    def start(self):
        """Start the worker threads. Safe to call more than once."""
        with self._state_lock:
            if self._accepting:
                return
            self._accepting = True
            self._started_at = time.time()
            self._threads = [
                threading.Thread(
                    target=self._worker_loop,
                    name=f"recroot-worker-{i + 1}",
                    daemon=True,   # never keeps the process alive at exit
                )
                for i in range(self._pool_size)
            ]
            threads = list(self._threads)
        for thread in threads:
            thread.start()
        logger.info("Resume worker pool started: %d threads, queue capacity %d.",
                    self._pool_size, self._queue_capacity)

    def stop(self, drain=False, timeout=10.0):
        """
        Stop accepting work and shut the threads down.

        drain=False (default): tasks still waiting in the queue are dropped.
            Their rows stay 'queued' in the database, so
            recover_unprocessed() picks them up on the next start.
        drain=True: wait (up to `timeout` seconds) for the queue to empty first.

        A task a worker is already processing is always allowed to finish
        (until `timeout` runs out; threads are daemons, so exit never hangs).
        """
        deadline = time.monotonic() + timeout
        with self._state_lock:
            self._accepting = False        # no new work from here on
            threads = list(self._threads)

        if drain:
            while time.monotonic() < deadline:
                with self._state_lock:
                    busy = self._active
                if not self._scheduler.pending_count() and not busy:
                    break
                time.sleep(0.05)

        self._discard_pending()
        self._scheduler.close()            # wakes every idle worker; they exit

        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

        with self._state_lock:
            self._threads = [t for t in self._threads if t.is_alive()]
        logger.info("Resume worker pool stopped.")

    def _discard_pending(self):
        """Empty the queue without processing it (used by stop())."""
        removed = self._scheduler.discard_all()
        with self._state_lock:
            for task in removed:
                self._in_flight.discard(task.application_id)
                entry = self._tasks.get(task.application_id)
                if entry is not None:
                    entry["state"] = STATE_DISCARDED
                    entry["finished_at"] = time.time()

    # --------------------------------------------------------
    # Producer side (called from Flask request threads)
    # --------------------------------------------------------

    def enqueue(self, application_id, metadata=None):
        """
        Queue one application for processing. Never blocks.

        Returns True if queued. Returns False if the pool is stopped, the
        application is already queued/being processed, or the queue is full.
        A False result is not an error for the caller: the application row is
        already saved with processing_status 'queued' and the recovery sweep
        will pick it up later.
        """
        return self._try_enqueue(application_id, metadata) == "queued"

    def _try_enqueue(self, application_id, metadata=None):
        """
        metadata (optional dict with applied_at, deadline, job_title) lets the
        recovery sweep avoid one query per task; otherwise it is read from the
        database here, OUTSIDE _state_lock (no database I/O under a lock).
        """
        application_id = int(application_id)
        with self._state_lock:
            if not self._accepting:
                return "stopped"
            if application_id in self._in_flight:
                self._stats["rejected_in_flight"] += 1
                return "in_flight"
            # Reserve the id so a concurrent enqueue of the same application
            # is rejected while we look up its scheduling data.
            self._in_flight.add(application_id)
            self._tasks[application_id] = {
                "application_id": application_id,
                "state": STATE_QUEUED,
                "queued_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "wait_seconds": None,
                "duration_seconds": None,
                "attempts": 0,
                "note": None,
                "worker": None,
                "dispatch_seq": None,
                "completion_seq": None,
            }
            self._tasks.move_to_end(application_id)
            self._evict_finished_locked()

        task = self._build_task(application_id, metadata)
        result = self._scheduler.add(task)

        with self._state_lock:
            if result == ADD_QUEUED:
                self._stats["enqueued"] += 1
                entry = self._tasks.get(application_id)
                if entry is not None:
                    entry["job_title"] = task.job_title
                    entry["deadline"] = task.deadline
                    entry["applied_at"] = task.applied_at
                return "queued"
            self._in_flight.discard(application_id)
            self._tasks.pop(application_id, None)
            if result == ADD_CLOSED:
                return "stopped"
            if result == "full":
                self._stats["rejected_full"] += 1
                logger.warning("Worker queue full; application %s left 'queued'.",
                               application_id)
                return "full"
            self._stats["rejected_in_flight"] += 1
            return "in_flight"

    def _build_task(self, application_id, metadata=None):
        """Scheduling record for one application. Never raises: if the data
        cannot be read the task is still scheduled (flagged metadata_missing)."""
        if metadata is None:
            try:
                metadata = self._lookup_task_metadata(application_id)
            except Exception:
                logger.exception("Could not read scheduling data for application %s.",
                                 application_id)
                metadata = None
        metadata = metadata or {}
        try:
            return make_task(application_id,
                             applied_at=metadata.get("applied_at"),
                             deadline=metadata.get("deadline"),
                             job_title=metadata.get("job_title"))
        except (TypeError, ValueError):
            logger.exception("Bad scheduling data for application %s.", application_id)
            return make_task(application_id)

    def _lookup_task_metadata(self, application_id):
        """applied_at, job deadline and title for one application (read only)."""
        with self._temporary_connection() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT a.applied_at, j.deadline, j.title AS job_title "
                    "FROM applications a LEFT JOIN job_postings j ON j.id = a.job_id "
                    "WHERE a.id = %s",
                    (application_id,),
                )
                row = cur.fetchone()
            db.rollback()
        return dict(row) if row else None

    def recover_unprocessed(self, include_failed=True):
        """
        Re-queue applications whose DB marker says they never finished:
        'queued' and 'processing' (lost on a restart), and optionally 'failed'.
        Oldest first. Stops early if the queue fills; the rest stay marked in
        the database and are picked up by a later sweep.

        Returns {"found", "enqueued", "already_in_flight", "left_for_later"}.
        """
        statuses = [DB_QUEUED, DB_PROCESSING]
        if include_failed:
            statuses.append(DB_FAILED)
        placeholders = ", ".join(["%s"] * len(statuses))

        with self._temporary_connection() as db:
            with db.cursor() as cur:
                cur.execute(
                    f"SELECT a.id, a.applied_at, j.deadline, j.title AS job_title "
                    f"FROM applications a LEFT JOIN job_postings j ON j.id = a.job_id "
                    f"WHERE a.processing_status IN ({placeholders}) "
                    f"ORDER BY a.applied_at, a.id",
                    tuple(statuses),
                )
                rows = list(cur.fetchall())
            db.rollback()

        ids = [row["id"] for row in rows]
        summary = {"found": len(ids), "enqueued": 0,
                   "already_in_flight": 0, "left_for_later": 0}
        for position, row in enumerate(rows):
            application_id = row["id"]
            result = self._try_enqueue(application_id, metadata=row)
            if result == "queued":
                summary["enqueued"] += 1
            elif result == "in_flight":
                summary["already_in_flight"] += 1
            else:  # "full" or "stopped": leave the remainder for a later sweep
                summary["left_for_later"] = len(ids) - position
                break
        logger.info("Recovery sweep: %s", summary)
        with self._state_lock:
            self._last_recovery = dict(summary, include_failed=bool(include_failed),
                                       at=time.time())
        return summary

    # --------------------------------------------------------
    # Status / statistics
    # --------------------------------------------------------

    def get_task_status(self, application_id):
        """Copy of the in-memory status entry for one application, or None."""
        with self._state_lock:
            entry = self._tasks.get(int(application_id))
            return dict(entry) if entry else None

    def get_stats(self):
        """Snapshot of pool health and throughput (used by Steps 36 and 39)."""
        with self._state_lock:
            stats = dict(self._stats)
            finished = stats["completed"] + stats["failed"] + stats["skipped"]
            snapshot = {
                "running": self._accepting,
                "pool_size": self._pool_size,
                "threads_alive": sum(1 for t in self._threads if t.is_alive()),
                "active_workers": self._active,
                "queue_depth": self._scheduler.pending_count(),
                "scheduling_algorithm": self._scheduler.get_algorithm(),
                "dispatch_window_seconds": self._scheduler.get_dispatch_window(),
                "queue_capacity": self._queue_capacity,
                "in_flight": len(self._in_flight),
                "uptime_seconds": (time.time() - self._started_at
                                   if self._started_at else 0.0),
                "avg_seconds": (stats["total_seconds"] / finished) if finished else 0.0,
                # read-only configuration and recovery info (deadlock dashboard)
                "max_retries": self._max_retries,
                "retry_backoff_seconds": self._retry_backoff,
                "lock_wait_timeout_seconds": self._lock_wait_timeout,
                "last_recovery": (dict(self._last_recovery)
                                  if self._last_recovery else None),
            }
        snapshot.update(stats)
        return snapshot

    # --------------------------------------------------------
    # Scheduling controls and inspection (used by /admin/scheduler)
    # --------------------------------------------------------

    def get_scheduling_algorithm(self):
        return self._scheduler.get_algorithm()

    def set_scheduling_algorithm(self, algorithm):
        """Switch FCFS / priority for every FUTURE selection. Tasks already
        taken by a worker are unaffected. Raises ValueError for an unknown
        name. Returns the previous algorithm."""
        return self._scheduler.set_algorithm(algorithm)

    def get_scheduler_snapshot(self):
        """Waiting applications in the order the scheduler will select them
        (same key function pop_next() uses), plus the task registry rows
        for applications that were already dispatched, newest dispatch last."""
        snapshot = self._scheduler.snapshot()
        with self._state_lock:
            dispatched = [dict(e) for e in self._tasks.values()
                          if e.get("dispatch_seq") is not None]
        dispatched.sort(key=lambda e: e["dispatch_seq"])
        snapshot["dispatched"] = dispatched
        return snapshot

    def _evict_finished_locked(self):
        """Keep the registry bounded by dropping the oldest finished entries."""
        if len(self._tasks) <= MAX_TRACKED_TASKS:
            return
        for application_id in list(self._tasks):
            if len(self._tasks) <= MAX_TRACKED_TASKS:
                break
            if self._tasks[application_id]["state"] in _FINISHED_STATES:
                del self._tasks[application_id]

    # --------------------------------------------------------
    # Per-thread database connection
    # --------------------------------------------------------

    def _open_connection(self):
        import pymysql
        import pymysql.cursors

        cfg = self._db_config
        kwargs = {}
        if self._lock_wait_timeout:
            # Fail a blocked row lock after N seconds (error 1205, retried
            # below) instead of waiting MySQL's default 50 s. Session-level:
            # affects only this pool's connections.
            kwargs["init_command"] = (
                f"SET SESSION innodb_lock_wait_timeout = {self._lock_wait_timeout}"
            )
        return pymysql.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,      # this module controls its own transactions
            connect_timeout=10,
            **kwargs,
        )

    def _get_connection(self):
        """This thread's own connection, reconnecting if it has gone away."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.ping(reconnect=True)
                return conn
            except Exception:
                self._drop_connection()
        conn = self._open_connection()
        self._local.conn = conn
        return conn

    def _drop_connection(self):
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _safe_rollback(self):
        """Roll back this thread's open transaction. Returns True if the
        rollback ran, False if it failed (the connection is then discarded),
        None if this thread has no connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return None
        try:
            conn.rollback()
            return True
        except Exception:
            self._drop_connection()
            return False

    @contextmanager
    def _temporary_connection(self):
        conn = self._open_connection()
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # --------------------------------------------------------
    # Worker thread
    # --------------------------------------------------------

    def _worker_loop(self):
        """Consumer: take tasks from the queue until the shutdown sentinel."""
        try:
            while True:
                # Blocks while nothing is waiting; the scheduler removes the
                # chosen task under its own lock, so no two workers get the
                # same one. None means the queue was closed (shutdown).
                decision = self._scheduler.pop_next()
                if decision is None:
                    return
                try:
                    self._handle_task(decision)
                except Exception:
                    # A bug in one task must never kill the thread.
                    logger.exception("Unhandled error in worker thread.")
        finally:
            self._drop_connection()

    def _handle_task(self, decision):
        application_id = decision.task.application_id
        began = time.monotonic()
        self._mark_started(decision)

        state, note = STATE_FAILED, "Processing did not complete."
        try:
            state, note = self._process_with_retries(application_id)
        except Exception as exc:  # defensive: _process_with_retries handles its own
            logger.exception("Task for application %s crashed.", application_id)
            state, note = STATE_FAILED, _truncate(f"{type(exc).__name__}: {exc}")
        finally:
            self._mark_finished(application_id, state, note, time.monotonic() - began)

    def _mark_started(self, decision):
        application_id = decision.task.application_id
        with self._state_lock:
            self._active += 1
            entry = self._tasks.setdefault(application_id, {
                "application_id": application_id, "queued_at": time.time(),
                "attempts": 0, "note": None,
            })
            entry["state"] = STATE_PROCESSING
            entry["started_at"] = time.time()
            entry["worker"] = threading.current_thread().name
            entry["dispatch_seq"] = decision.dispatch_seq
            entry["algorithm"] = decision.algorithm
            entry["bucket_label"] = BUCKET_LABELS[decision.bucket]
            entry["passed_over"] = decision.passed_over
            entry.setdefault("job_title", decision.task.job_title)
            entry.setdefault("deadline", decision.task.deadline)
            entry.setdefault("applied_at", decision.task.applied_at)
            entry["wait_seconds"] = entry["started_at"] - entry.get("queued_at", entry["started_at"])

    def _mark_finished(self, application_id, state, note, duration):
        with self._state_lock:
            self._active -= 1
            self._in_flight.discard(application_id)   # allow re-queueing again
            self._completion_seq += 1
            entry = self._tasks.get(application_id)
            if entry is not None:
                entry["completion_seq"] = self._completion_seq
                entry["state"] = state
                entry["finished_at"] = time.time()
                entry["duration_seconds"] = duration
                entry["note"] = note
            if state == STATE_DONE:
                self._stats["completed"] += 1
            elif state == STATE_SKIPPED:
                self._stats["skipped"] += 1
            else:
                self._stats["failed"] += 1
            self._stats["total_seconds"] += duration
            self._stats["max_seconds"] = max(self._stats["max_seconds"], duration)
            self._evict_finished_locked()

    # --------------------------------------------------------
    # Retry wrapper
    # --------------------------------------------------------

    def _process_with_retries(self, application_id):
        """
        Run the pipeline, retrying only transient errors with exponential
        backoff: MySQL deadlock (1213), lock wait timeout (1205), a lost
        connection, or a timed-out in-process lock (LockAcquireTimeout).
        Every retry first rolls the failed transaction back and starts from a
        fresh connection, so the whole pipeline is re-run from its first
        statement. Retries are bounded by max_retries.

        For 1213/1205 the backoff gets a little random jitter (two victims of
        the same deadlock must not wake up together and collide again) and
        the event is counted in modules.db_retry.lock_events - real events
        only. Returns (state, note); never raises for ordinary failures.
        """
        total_attempts = self._max_retries + 1
        hit_lock_error = False
        for attempt in range(1, total_attempts + 1):
            with self._state_lock:
                entry = self._tasks.get(application_id)
                if entry is not None:
                    entry["attempts"] = attempt
            try:
                db = self._get_connection()
                outcome = self._process_once(db, application_id)
                if hit_lock_error:
                    lock_events.record_recovered(where=f"application #{application_id}",
                                                 attempts=attempt)
                return outcome
            except Exception as exc:
                rolled_back = self._safe_rollback()
                lock_error = is_lock_error(exc)
                retryable = (lock_error or isinstance(exc, LockAcquireTimeout)
                             or _is_transient_db_error(exc))
                will_retry = retryable and attempt < total_attempts
                delay = None
                if will_retry:
                    if lock_error:
                        delay = backoff_delay(attempt, base=self._retry_backoff,
                                              cap=MAX_RETRY_DELAY, jitter=0.25)
                    else:
                        delay = self._retry_backoff * (2 ** (attempt - 1))
                where = f"application #{application_id}"
                if lock_error:
                    hit_lock_error = True
                    lock_events.record_error(
                        exc, where=where, attempt=attempt,
                        total_attempts=total_attempts, retry_in=delay,
                        rolled_back=rolled_back)
                elif isinstance(exc, LockAcquireTimeout):
                    lock_events.record_python_lock_timeout(where)

                if will_retry:
                    logger.warning(
                        "Transient error on application %s (attempt %d/%d): %s "
                        "- rolled back, retrying in %.1fs", application_id, attempt,
                        total_attempts, exc, delay)
                    with self._state_lock:
                        self._stats["retries"] += 1
                    if lock_error:
                        lock_events.record_retry()
                    self._drop_connection()       # force a fresh connection
                    time.sleep(delay)
                    continue
                if lock_error:
                    lock_events.record_exhausted()
                logger.exception("Processing failed for application %s.", application_id)
                note = _truncate(f"{type(exc).__name__}: {exc}")
                self._record_failure(application_id, note)
                return STATE_FAILED, note
        return STATE_FAILED, "Processing did not complete."  # pragma: no cover

    def _record_failure(self, application_id, note):
        """Best effort: mark the row failed and audit it after retries ran out.
        This one short transaction (applications row first, audit_log last)
        is itself retried on deadlock / lock timeout, so a lock conflict does
        not leave the row stuck in 'processing'."""
        def write_failure():
            db = self._get_connection()
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE applications SET processing_status = %s, "
                    "processing_note = %s, processed_at = NOW() WHERE id = %s",
                    (DB_FAILED, note, application_id),
                )
                cur.execute(
                    "INSERT INTO audit_log (user_id, action, details) VALUES (NULL, %s, %s)",
                    ("system_process_failed",
                     _truncate(f"Application #{application_id}: {note}", 1000)),
                )
            db.commit()

        try:
            run_with_lock_retry(
                write_failure, self._safe_rollback,
                max_retries=self._max_retries,
                base_delay=self._retry_backoff, max_delay=MAX_RETRY_DELAY,
                sleep=lambda seconds: time.sleep(seconds),
                where=f"recording failure of application #{application_id}",
            )
        except Exception:
            logger.exception("Could not record failure for application %s.", application_id)
            self._safe_rollback()
            self._drop_connection()

    # --------------------------------------------------------
    # The pipeline (Section 13)
    # --------------------------------------------------------

    def _process_once(self, db, application_id):
        """One full pass of the pipeline for one application. Returns (state, note)."""
        began = time.monotonic()

        loaded = self._load_application(db, application_id)
        if loaded is None:
            db.rollback()
            return STATE_SKIPPED, "Application no longer exists."
        application, job, file_hash, version_found = loaded
        candidate_id = application["candidate_id"]
        resume_path = application["resume_path"]

        # Make "processing" visible to the dashboards straight away.
        with db.cursor() as cur:
            cur.execute(
                "UPDATE applications SET processing_status = %s, processing_note = NULL "
                "WHERE id = %s",
                (DB_PROCESSING, application_id),
            )
        db.commit()

        notes = []          # informational, e.g. "Not scored: ..."
        stage_errors = []   # a stage raised; makes the final status 'failed'

        # ---- Stage 0: parse once, share with every stage -------------------
        parsed, parse_note = self._parse_resume_file(resume_path)
        if parse_note:
            notes.append(parse_note)

        # ---- Stage 1: duplicate detection (short critical section) --------
        def duplicate_stage():
            with self._dedup_lock:
                if parsed is not None and version_found:
                    # Commits the fingerprint so the next worker can see it.
                    index_resume(db, candidate_id, resume_path, file_hash,
                                 parsed["resume_text"])
                # End any open transaction so the comparison reads a fresh
                # snapshot that includes every fingerprint committed so far.
                db.commit()
                result = check_duplicates(
                    db, candidate_id, resume_path, file_hash=file_hash,
                    threshold=self._near_duplicate_threshold,
                )
                # Short transactions: the comparison only READ. End that
                # read-only transaction now instead of keeping its snapshot
                # open through the consistency and scoring stages.
                db.commit()
            return application_duplicate_fields(result)

        # ---- Stage 2: consistency analysis (pure, runs in parallel) -------
        def consistency_stage():
            result = check_consistency(
                {"name": application["candidate_name"],
                 "email": application["candidate_email"]},
                parsed,
            )
            return application_consistency_fields(result)

        # ---- Stage 3: scoring (pure, runs in parallel) ---------------------
        def scoring_stage():
            return score_application(parsed, job, weights=self._weights)

        duplicate_fields = self._run_stage("duplicate detection", duplicate_stage,
                                           application_id, stage_errors)
        consistency_fields = self._run_stage("consistency analysis", consistency_stage,
                                             application_id, stage_errors)
        score_result = self._run_stage("scoring", scoring_stage,
                                       application_id, stage_errors)

        if score_result is not None and not score_result.get("scored"):
            notes.append(f"Not scored: {score_result.get('reason')}")

        # ---- Final transaction: everything or nothing ----------------------
        status = DB_FAILED if stage_errors else DB_DONE
        note = _truncate("; ".join(
            notes + [f"{err}" for err in stage_errors]
        )) or None

        sets = ["processing_status = %s", "processing_note = %s",
                "processed_at = NOW()"]
        params = [status, note]
        if duplicate_fields is not None:
            sets += ["duplicate_flag = %s", "duplicate_similarity = %s"]
            params += [duplicate_fields[0], duplicate_fields[1]]
        if consistency_fields is not None:
            sets += ["consistency_flag = %s", "consistency_notes = %s"]
            params += [consistency_fields[0], consistency_fields[1]]
        params.append(application_id)

        elapsed = time.monotonic() - began
        if score_result is None:
            score_text = "score error"
        elif score_result.get("scored"):
            score_text = f"score {score_result['total']}/100"
        else:
            score_text = "not scored"

        with db.cursor() as cur:
            if score_result is not None:
                save_score(db, application_id, score_result)   # no-op if unscored
            cur.execute(
                f"UPDATE applications SET {', '.join(sets)} WHERE id = %s",
                tuple(params),
            )
            cur.execute(
                "INSERT INTO audit_log (user_id, action, details) VALUES (NULL, %s, %s)",
                ("system_process_application",
                 f"Application #{application_id}: {status}; {score_text}; "
                 f"{elapsed:.2f}s"),
            )
        db.commit()

        return (STATE_DONE if status == DB_DONE else STATE_FAILED), note

    def _run_stage(self, name, func, application_id, stage_errors):
        """
        Run one pipeline stage in isolation. A transient DB error (deadlock,
        lock timeout, lost connection) or a timed-out in-process lock is
        re-raised so the whole task is rolled back and retried; any other
        exception is recorded and the pipeline carries on (returns None for
        this stage).
        """
        try:
            return func()
        except Exception as exc:
            if _is_transient_db_error(exc) or isinstance(exc, LockAcquireTimeout):
                raise
            logger.exception("Stage '%s' failed for application %s.", name, application_id)
            stage_errors.append(f"{name} error: {type(exc).__name__}: {exc}")
            return None

    def _load_application(self, db, application_id):
        """
        Returns (application, job, file_hash, version_found) or None if the
        application (or its job) no longer exists.
        """
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.candidate_id, a.job_id, a.resume_path,
                       u.name AS candidate_name, u.email AS candidate_email
                FROM applications a
                JOIN users u ON u.id = a.candidate_id
                WHERE a.id = %s
                """,
                (application_id,),
            )
            application = cur.fetchone()
            if application is None:
                return None

            cur.execute("SELECT * FROM job_postings WHERE id = %s",
                        (application["job_id"],))
            job = cur.fetchone()
            if job is None:
                return None

            cur.execute(
                "SELECT file_hash FROM resume_versions "
                "WHERE candidate_id = %s AND resume_path = %s LIMIT 1",
                (application["candidate_id"], application["resume_path"]),
            )
            version = cur.fetchone()

        return (application, job,
                version["file_hash"] if version else None,
                version is not None)

    def _parse_resume_file(self, relative_path):
        """
        Parse the stored PDF. Returns (parsed_or_None, note_or_None).
        Never raises: an unreadable resume is a normal outcome (the other
        stages report it as "not checked"/"not scored"), not a pipeline error.
        resume_path is stored relative to the static folder; like
        candidate.py we resolve it by file name under UPLOAD_FOLDER.
        """
        absolute_path = os.path.join(self._upload_folder, os.path.basename(relative_path or ""))
        try:
            return parse_resume(absolute_path), None
        except ResumeParseError as exc:
            logger.warning("Resume parse failed for %s: %s", relative_path, exc)
            return None, "Resume could not be read."
        except Exception:
            logger.exception("Unexpected error parsing %s", relative_path)
            return None, "Resume could not be read."


# ============================================================
# Module-level singleton (single-process design)
# ============================================================

_pool = None
_pool_lock = threading.Lock()


def init_worker(app, start=True, recover=True):
    """
    Create (once) and start the process-wide worker pool from the Flask
    app's config. Call from create_app(). Returns the pool.

    Config keys used (all already in config.py except the last two, which
    have defaults here): DB_CONFIG, UPLOAD_FOLDER, WORKER_THREAD_POOL_SIZE,
    WORKER_QUEUE_MAX_SIZE, NEAR_DUPLICATE_THRESHOLD,
    WORKER_MAX_RETRIES, WORKER_RETRY_BACKOFF_SECONDS, and (optional, with
    defaults here) WORKER_LOCK_WAIT_TIMEOUT_SECONDS,
    WORKER_DEDUP_LOCK_TIMEOUT_SECONDS.

    recover=True runs the startup sweep. A failure there (e.g. the
    processing_* columns not added yet) is logged and never stops the app.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            cfg = app.config
            _pool = ResumeWorkerPool(
                db_config=cfg["DB_CONFIG"],
                upload_folder=cfg["UPLOAD_FOLDER"],
                pool_size=cfg.get("WORKER_THREAD_POOL_SIZE", DEFAULT_POOL_SIZE),
                queue_max_size=cfg.get("WORKER_QUEUE_MAX_SIZE", DEFAULT_QUEUE_SIZE),
                near_duplicate_threshold=cfg.get("NEAR_DUPLICATE_THRESHOLD", 85),
                max_retries=cfg.get("WORKER_MAX_RETRIES", DEFAULT_MAX_RETRIES),
                retry_backoff=cfg.get("WORKER_RETRY_BACKOFF_SECONDS", DEFAULT_RETRY_BACKOFF),
                weights=None,   # default scoring weights for now
                scheduling_algorithm=cfg.get("WORKER_SCHEDULING_ALGORITHM", ALGORITHM_FCFS),
                dispatch_window_seconds=cfg.get("WORKER_DISPATCH_WINDOW_SECONDS", 0),
                lock_wait_timeout=cfg.get("WORKER_LOCK_WAIT_TIMEOUT_SECONDS",
                                          DEFAULT_LOCK_WAIT_TIMEOUT),
                dedup_lock_timeout=cfg.get("WORKER_DEDUP_LOCK_TIMEOUT_SECONDS",
                                           DEFAULT_DEDUP_LOCK_TIMEOUT),
            )
        pool = _pool

    if start:
        pool.start()
        if recover:
            try:
                pool.recover_unprocessed()
            except Exception:
                logger.exception("Startup recovery sweep failed; continuing without it.")
    return pool


def get_worker():
    """The process-wide pool, or None if init_worker() hasn't run."""
    return _pool


def enqueue_application(application_id, metadata=None):
    """Queue one application. False if the pool is missing, stopped or full.
    metadata (applied_at, deadline, job_title) is optional; it is read from
    the database when omitted."""
    pool = _pool
    if pool is None:
        logger.warning("enqueue_application(%s) called before init_worker().",
                       application_id)
        return False
    return pool.enqueue(application_id, metadata)


def recover_unprocessed(include_failed=True):
    pool = _pool
    if pool is None:
        raise RuntimeError("Worker pool is not initialised.")
    return pool.recover_unprocessed(include_failed=include_failed)


def get_worker_stats():
    pool = _pool
    return pool.get_stats() if pool is not None else {"running": False}


def get_scheduling_algorithm():
    """Active algorithm name, or None if the worker pool is not running."""
    pool = _pool
    return pool.get_scheduling_algorithm() if pool is not None else None


def set_scheduling_algorithm(algorithm):
    """Switch the live pool's algorithm. Raises RuntimeError if no pool,
    ValueError for an unknown algorithm. Returns the previous algorithm."""
    pool = _pool
    if pool is None:
        raise RuntimeError("Worker pool is not running.")
    return pool.set_scheduling_algorithm(algorithm)


def get_scheduler_snapshot():
    """Queue snapshot for the admin page, or None if no pool is running."""
    pool = _pool
    return pool.get_scheduler_snapshot() if pool is not None else None


def get_task_status(application_id):
    pool = _pool
    return pool.get_task_status(application_id) if pool is not None else None


def shutdown_worker(drain=False, timeout=10.0):
    """Stop the pool (e.g. at interpreter exit or in tests)."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.stop(drain=drain, timeout=timeout)