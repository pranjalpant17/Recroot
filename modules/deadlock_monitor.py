"""
Recroot - Deadlock Monitoring & Recovery view-model (admin dashboard)

Turns REAL data into a plain dict for templates/admin/deadlock.html. Three
sources, all read-only, none invented:

  1. modules.db_retry.lock_events.snapshot()
       Counters and an event log created only when the worker's retry code
       actually caught MySQL error 1213 (deadlock) or 1205 (lock wait timeout),
       rolled back, retried, recovered or gave up. In-memory, this process
       only, reset on restart.
  2. modules.worker.get_worker_stats()
       The live worker pool: status, threads, queue, retry configuration and
       the last recovery sweep. Status text comes from
       modules.worker_monitor.build_monitor_view, the same code as the
       Worker Monitor page.
  3. The database (optional, SELECT only): applications per processing_status
       and the most recent 'system_process_failed' audit rows. These survive
       restarts. They record failures from ANY cause; a row whose text carries
       MySQL error 1213 or 1205 is marked as lock-related.

  4. The result of the last REAL DEADLOCK TEST (modules/real_deadlock_test.py),
       kept in memory so the page can keep showing it. It is a genuine MySQL
       deadlock produced on a dedicated test table by an admin on purpose. It
       is held and displayed separately: it has its own private counters and
       is never added to the worker's counters in (1).

The in-memory deadlock SIMULATION (modules/deadlock_demo.py) is deliberately
not imported here: nothing on the monitoring side can be influenced by it.

No function here writes to the database or changes the worker.
"""

import copy
import logging
import threading

from modules.db_retry import lock_events
from modules.worker_monitor import (
    DEFAULT_REFRESH_SECONDS,
    build_monitor_view,
    get_processing_counts,
)

logger = logging.getLogger("recroot.deadlock_monitor")

__all__ = ["DEFAULT_REFRESH_SECONDS", "EVENTS_SHOWN", "build_dashboard",
           "get_dashboard_view", "get_persisted_view",
           "remember_real_test", "get_real_test_view", "build_real_test_view"]

EVENTS_SHOWN = 50
RECENT_FAILURES_SHOWN = 10

KIND_LABELS = {
    "deadlock": "Deadlock (MySQL 1213)",
    "lock_timeout": "Lock wait timeout (MySQL 1205)",
    "python_lock_timeout": "In-process lock timeout (Python)",
    "recovered": "Recovered",
}


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _health(real, pool_present):
    """Overall state derived only from the real counters."""
    exhausted = _to_int(real.get("exhausted"))
    observed = _to_int(real.get("total_mysql_events")) + _to_int(real.get("python_lock_timeouts"))
    if exhausted:
        return {
            "state": "attention",
            "label": "Attention",
            "message": (f"{exhausted} task(s) used up every retry after a lock error. They "
                        "were marked failed and are re-queued by the next recovery sweep "
                        "(it runs when the server starts)."),
        }
    if observed:
        return {
            "state": "recovered",
            "label": "Conflicts observed, none lost",
            "message": ("Lock conflicts were observed and handled by rollback and retry. "
                        "No task has used up its retries."),
        }
    message = "No MySQL deadlock or lock wait timeout has been observed by this server process."
    if not pool_present:
        message += " The worker pool is not running here, so nothing is being watched."
    return {"state": "quiet", "label": "No conflicts observed", "message": message}


def _event_rows(events):
    rows = []
    for event in events[:EVENTS_SHOWN]:
        attempt, total = event.get("attempt"), event.get("total_attempts")
        if event.get("type") == "recovered":
            attempt_text = f"attempt {attempt}" if attempt else "—"
        elif attempt and total:
            attempt_text = f"{attempt} of {total}"
        else:
            attempt_text = "—"
        rows.append({
            "seq": event.get("seq"),
            "at_text": event.get("at_text", ""),
            "kind": event.get("kind"),
            "kind_label": KIND_LABELS.get(event.get("kind"), str(event.get("kind"))),
            "code": event.get("code"),
            "where": event.get("where") or "—",
            "thread": event.get("thread") or "—",
            "attempt_text": attempt_text,
            "action": event.get("action") or "—",
            "message": event.get("message") or "",
            "is_error": event.get("type") == "error",
        })
    return rows


def build_dashboard(real, stats, pool_present, module_error=None):
    """
    real          lock_events.snapshot()
    stats         get_worker_stats() (may be just {"running": False})
    pool_present  True if a worker pool exists in this process
    """
    real = real or {}
    stats = stats or {}
    worker = build_monitor_view(pool_present, stats, module_error=module_error)

    recovered = _to_int(real.get("recovered"))
    exhausted = _to_int(real.get("exhausted"))
    concluded = recovered + exhausted

    worker.update({
        "max_retries": stats.get("max_retries"),
        "retry_backoff_seconds": stats.get("retry_backoff_seconds"),
        "lock_wait_timeout_seconds": stats.get("lock_wait_timeout_seconds"),
        "last_recovery": stats.get("last_recovery"),
        "scheduling_algorithm": stats.get("scheduling_algorithm"),
    })
    last = worker["last_recovery"]
    if last and last.get("at"):
        from datetime import datetime
        last["at_text"] = datetime.fromtimestamp(last["at"]).strftime("%Y-%m-%d %H:%M:%S")

    return {
        "health": _health(real, pool_present),
        "real": real,
        "events": _event_rows(real.get("events") or []),
        "events_total_kept": len(real.get("events") or []),
        "events_shown_limit": EVENTS_SHOWN,
        # Of the tasks that hit a lock error AND have since finished, how many recovered
        "tasks_concluded": concluded,
        "recovery_percent": (round(recovered * 100.0 / concluded) if concluded else None),
        "worker": worker,
    }


def get_dashboard_view():
    """Snapshot of the live process. Never raises."""
    real = lock_events.snapshot()
    try:
        from modules.worker import get_worker, get_worker_stats
    except Exception as exc:
        logger.exception("modules.worker could not be imported.")
        return build_dashboard(real, {}, False, module_error=type(exc).__name__)
    return build_dashboard(real, get_worker_stats(), get_worker() is not None)


def get_persisted_view(open_db):
    """
    Read-only look at what the database remembers. open_db is a zero-argument
    callable returning a connection (so the caller decides how to obtain it).
    Never raises: any failure becomes {"available": False, "error": <type>}.
    """
    try:
        db = open_db()
        counts = get_processing_counts(db)
        failures = []
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, timestamp, details FROM audit_log "
                "WHERE action = 'system_process_failed' "
                "ORDER BY id DESC LIMIT %s",
                (RECENT_FAILURES_SHOWN,),
            )
            for row in cur.fetchall():
                details = str(row.get("details") or "")
                failures.append({
                    "id": row.get("id"),
                    "timestamp": row.get("timestamp"),
                    "details": details,
                    "lock_related": ("(1213," in details) or ("(1205," in details),
                })
        return {"available": True, "error": None,
                "processing_counts": counts, "recent_failures": failures}
    except Exception as exc:
        logger.warning("Persisted deadlock view unavailable: %s", type(exc).__name__)
        return {"available": False, "error": type(exc).__name__,
                "processing_counts": None, "recent_failures": []}


# ============================================================
# Real deadlock test result (separate from worker counters and from the simulation)
# ============================================================

OUTCOME_LABELS = {
    "verified": "Verified: genuine MySQL deadlock, rolled back, retried, recovered",
    "partial": "Deadlock occurred, but recovery checks did not all pass",
    "not_reproduced": "No deadlock reproduced",
    "error": "Test could not run",
    "busy": "Not run: another real deadlock test is in progress",
}
CHECK_MARKS = {"pass": "PASS", "fail": "FAIL", "warn": "NOTE", "n/a": "n/a"}

_real_test_lock = threading.Lock()
_last_real_test = None


def remember_real_test(result):
    """Keep the latest real-test result (a private copy) for the dashboard."""
    global _last_real_test
    with _real_test_lock:
        _last_real_test = copy.deepcopy(result)


def build_real_test_view(result):
    """Display data for one real-test result dict; None if there is none."""
    if not result:
        return None
    threads = []
    for t in result.get("threads") or []:
        first_error = t.get("first_error")
        if first_error:
            role = (f"victim: MySQL error {first_error['code']}, rolled back"
                    + (", then retried and committed" if t.get("committed") else ", did not recover"))
        elif t.get("committed"):
            role = "survivor: proceeded once the other transaction was rolled back"
        else:
            role = "did not complete"
        threads.append(dict(t, role=role))
    return {
        "outcome": result.get("outcome"),
        "outcome_label": OUTCOME_LABELS.get(result.get("outcome"), str(result.get("outcome"))),
        "summary": result.get("summary") or "",
        "error": result.get("error"),
        "started_at_text": result.get("started_at_text"),
        "database": result.get("database"),
        "table": result.get("table"),
        "run_id": result.get("run_id"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "table_created_now": result.get("table_created_now"),
        "server": result.get("server") or {},
        "checks": [dict(c, mark=CHECK_MARKS.get(c.get("status"), "?"))
                   for c in result.get("checks") or []],
        "timeline": result.get("events") or [],
        "test_counters": result.get("test_counters") or {},
        # the test's own real MySQL events, formatted by the same code as the worker's
        "test_event_rows": _event_rows(result.get("test_events") or []),
        "threads": threads,
        "final_rows": result.get("final_rows") or [],
        "innodb_report": result.get("innodb_report"),
        "innodb_report_note": result.get("innodb_report_note") or "",
        "cleanup": result.get("cleanup") or {},
    }


def get_real_test_view():
    with _real_test_lock:
        result = copy.deepcopy(_last_real_test)
    return build_real_test_view(result)
