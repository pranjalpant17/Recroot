"""
Recroot — Worker Monitor view-model (Step 42, Part A)

Turns the real, public monitoring API of modules/worker.py into a plain dict
that templates/admin/worker.html can render. Nothing here is invented: every
number comes from

    modules.worker.get_worker()        -> the pool, or None if not initialised
    modules.worker.get_worker_stats()  -> ResumeWorkerPool.get_stats() snapshot

Fields actually provided by get_worker_stats() (and used below):
    running, pool_size, threads_alive, active_workers, queue_depth,
    queue_capacity, in_flight, uptime_seconds, avg_seconds,
    enqueued, completed, failed, skipped, retries,
    rejected_full, rejected_in_flight, total_seconds, max_seconds

NOT provided by the worker, therefore NOT shown as numbers:
    * average / maximum WAITING time (the worker records wait time only per
      task inside its private in-memory registry; there is no aggregate and
      no public function that lists tasks);
    * a scheduling-policy setting (the worker has none; see SCHEDULING_POLICY).

The statistics live in the memory of the server process that runs the worker
pool. They reset on restart and are not shared between several server
processes (the worker is documented as a single-process design).

The one database query here (processing-status counts) uses the same
get_db() connection pattern as the other admin routes.
"""

import logging

logger = logging.getLogger("recroot.worker_monitor")

# ---------------------------------------------------------------------------
# Scheduling policy
# ---------------------------------------------------------------------------
# modules/worker.py feeds its threads from one bounded queue.Queue, i.e. plain
# first-in, first-out (FCFS), non-preemptive. There is no priority queue and no
# policy setting yet (priority scheduling is Step 41, not implemented). If
# Step 41 later adds a configurable policy, read it from the worker here
# instead of this constant.
SCHEDULING_POLICY = "FIFO"
SCHEDULING_POLICY_DETAIL = (
    "First-come, first-served: one bounded queue, tasks are taken in the order "
    "they were enqueued. Priority scheduling is not implemented."
)

# The live policy comes from stats["scheduling_algorithm"] (set by
# modules/worker.py from modules/scheduler.py). The constants above are only
# the fallback when the worker reports nothing.
POLICY_NAMES = {"fcfs": "FCFS", "priority": "Priority (deadline)"}
POLICY_DETAILS = {
    "fcfs": "First-come, first-served: waiting applications are taken in order "
            "of applied_at, then application id.",
    "priority": "Deadline priority: upcoming deadlines first (earliest first), "
                "then no deadline, then expired; ties by applied_at, then id.",
}

# Status values of applications.processing_status (schema.sql), in display order.
PROCESSING_STATUSES = ("queued", "processing", "done", "failed")

DEFAULT_REFRESH_SECONDS = 5


# ---------------------------------------------------------------------------
# Small pure helpers (unit-testable without Flask or MySQL)
# ---------------------------------------------------------------------------

def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    # NaN / inf must never reach the page
    return result if result == result and result not in (float("inf"), float("-inf")) else default


def _percent(part, whole):
    """Whole-number percentage, clamped to 0..100; 0 when whole is 0."""
    if whole <= 0:
        return 0
    return max(0, min(100, int(round(part * 100.0 / whole))))


def format_seconds(seconds):
    """Human-readable duration for task timings, e.g. '0.42 s' or '1m 12s'."""
    seconds = _to_float(seconds)
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}m {rest:02d}s"


def format_uptime(seconds):
    """Compact uptime, e.g. '2h 05m', '3m 10s', '12s'."""
    total = max(0, int(_to_float(seconds)))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def build_monitor_view(pool_present, stats, module_error=None):
    """
    Build the template data from a stats snapshot.

    pool_present  True if get_worker() returned a pool.
    stats         the dict from get_worker_stats() (may be just {"running": False}).
    module_error  text if modules.worker could not be imported at all.
    """
    stats = stats or {}

    pool_size = _to_int(stats.get("pool_size"))
    threads_alive = _to_int(stats.get("threads_alive"))
    active = max(0, _to_int(stats.get("active_workers")))
    depth = max(0, _to_int(stats.get("queue_depth")))
    capacity = max(0, _to_int(stats.get("queue_capacity")))
    in_flight = max(0, _to_int(stats.get("in_flight")))

    enqueued = _to_int(stats.get("enqueued"))
    completed = _to_int(stats.get("completed"))
    failed = _to_int(stats.get("failed"))
    skipped = _to_int(stats.get("skipped"))
    finished = completed + failed + skipped

    running = bool(stats.get("running")) and pool_present

    # ---- overall state -------------------------------------------------
    if module_error:
        status, label = "unavailable", "Unavailable"
        message = "The worker module could not be loaded, so no statistics exist."
    elif not pool_present:
        status, label = "not_initialised", "Not initialised"
        message = (
            "No worker pool exists in this server process. It is not started "
            "when WORKER_AUTOSTART is off, in testing mode, in the debug "
            "reloader's parent process, or for CLI commands such as init-db."
        )
    elif running:
        status, label = "running", "Running"
        message = "The worker pool is accepting and processing resumes."
    else:
        status, label = "stopped", "Stopped"
        message = "The worker pool exists but is not accepting new work."

    # ---- warnings (only things that can actually be detected) -----------
    warnings = []
    if running and pool_size and threads_alive < pool_size:
        warnings.append(
            f"Only {threads_alive} of {pool_size} worker threads are alive."
        )
    if running and capacity and depth >= capacity:
        warnings.append(
            "The queue is full: new applications stay 'queued' in the database "
            "until the recovery sweep picks them up."
        )
    rejected_full = _to_int(stats.get("rejected_full"))
    if rejected_full:
        warnings.append(
            f"{rejected_full} task(s) were rejected because the queue was full."
        )

    if capacity and depth >= capacity:
        queue_state = "full"
    elif depth == 0:
        queue_state = "empty"
    else:
        queue_state = "waiting"

    return {
        "status": status,
        "status_label": label,
        "status_message": message,
        "module_error": module_error,
        "warnings": warnings,

        "policy": POLICY_NAMES.get(stats.get("scheduling_algorithm"), SCHEDULING_POLICY),
        "policy_detail": POLICY_DETAILS.get(stats.get("scheduling_algorithm"),
                                            SCHEDULING_POLICY_DETAIL),

        # pool
        "pool_size": pool_size,
        "threads_alive": threads_alive,
        "active_workers": active,
        "idle_workers": max(0, threads_alive - active),   # derived
        "worker_pct": _percent(active, pool_size),        # derived
        "in_flight": in_flight,
        "uptime": format_uptime(stats.get("uptime_seconds")) if running else "—",

        # queue
        "queue_depth": depth,
        "queue_capacity": capacity,
        "queue_pct": _percent(depth, capacity),           # derived
        "queue_state": queue_state,

        # counters
        "enqueued": enqueued,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "finished": finished,
        "retries": _to_int(stats.get("retries")),
        "rejected_full": rejected_full,
        "rejected_in_flight": _to_int(stats.get("rejected_in_flight")),

        # timings (processing only; the worker keeps no aggregate wait time)
        "has_timings": finished > 0,
        "avg_processing": format_seconds(stats.get("avg_seconds")),
        "max_processing": format_seconds(stats.get("max_seconds")),
        "total_processing": format_seconds(stats.get("total_seconds")),

        "has_activity": bool(enqueued or finished or active or depth),
    }


# ---------------------------------------------------------------------------
# Real data access
# ---------------------------------------------------------------------------

def get_monitor_view():
    """
    Snapshot of the live worker pool, using the worker's real public API.
    Never raises: a missing module or pool becomes a clear page state.
    """
    try:
        from modules.worker import get_worker, get_worker_stats
    except Exception as exc:  # ImportError, or a failure while importing deps
        logger.exception("modules.worker could not be imported.")
        return build_monitor_view(False, {}, module_error=type(exc).__name__)

    pool_present = get_worker() is not None
    stats = get_worker_stats()
    return build_monitor_view(pool_present, stats)


def get_processing_counts(db):
    """
    Persistent view of the pipeline: applications per processing_status,
    straight from the database (survives restarts, unlike the in-memory
    counters). Returns {'queued': n, 'processing': n, 'done': n, 'failed': n,
    'total': n}; every status is present even when its count is zero.
    """
    counts = {status: 0 for status in PROCESSING_STATUSES}
    with db.cursor() as cur:
        cur.execute(
            "SELECT processing_status, COUNT(*) AS total "
            "FROM applications GROUP BY processing_status"
        )
        for row in cur.fetchall():
            key = row["processing_status"]
            if key in counts:
                counts[key] = _to_int(row["total"])
    counts["total"] = sum(counts[s] for s in PROCESSING_STATUSES)
    return counts