"""
Recroot - Scheduling page data (no Flask, no MySQL)

Builds the dict that templates/admin/scheduler.html renders, from the real
public API of modules/worker.py. Nothing is simulated: the queue positions
come from SchedulerQueue.snapshot(), which uses the same key function the
worker threads use when they pick their next task.
"""

from modules.scheduler import ALGORITHM_LABELS, ALGORITHMS


def build_scheduler_view(snapshot, dispatched_limit=20):
    """
    snapshot: dict from get_scheduler_snapshot(), or None when the worker
    pool is not running in this process.
    """
    algorithms = [(name, ALGORITHM_LABELS[name]) for name in ALGORITHMS]
    if snapshot is None:
        return {
            "available": False,
            "algorithm": None,
            "algorithm_label": None,
            "algorithms": algorithms,
            "today": None,
            "capacity": 0,
            "dispatch_window_seconds": 0.0,
            "dispatch_window_remaining": 0.0,
            "waiting": [],
            "dispatched": [],
        }

    waiting = sorted(snapshot["tasks"], key=lambda row: row["position"])
    dispatched = list(snapshot.get("dispatched", []))[-int(dispatched_limit):]
    return {
        "available": True,
        "algorithm": snapshot["algorithm"],
        "algorithm_label": ALGORITHM_LABELS[snapshot["algorithm"]],
        "algorithms": algorithms,
        "today": snapshot["today"],
        "capacity": snapshot["capacity"],
        "dispatch_window_seconds": snapshot.get("dispatch_window_seconds", 0.0),
        "dispatch_window_remaining": snapshot.get("dispatch_window_remaining", 0.0),
        "waiting": waiting,
        "dispatched": dispatched,
        "dispatched_total": snapshot["dispatched_total"],
    }
