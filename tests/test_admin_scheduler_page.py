"""
Route + template tests for /admin/scheduler.

A minimal Flask app is built from the real auth and admin blueprints and the
real templates. The database is never touched: log_action is replaced by a
recorder, and the worker pool is a FakePool (see test_worker_scheduling.py)
installed as the process-wide pool, so the page reads the real
get_scheduler_snapshot() output.
"""

import os
import re
import unittest
from unittest import mock

from flask import Flask

from modules import worker as worker_module
from modules.admin import admin_bp
from modules.auth import auth_bp
from modules.worker_monitor import build_monitor_view
from tests.test_worker_scheduling import (
    ARRIVAL, BLOCKER, MIXED, FakePool, meta, wait_until,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_app():
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"),
                static_folder=os.path.join(ROOT, "static"))
    app.config.update(SECRET_KEY="test", TESTING=True)
    app.jinja_env.globals["csrf_token"] = lambda: "test-token"
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    return app


def login(client, role="admin"):
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["role"] = role
        sess["name"] = "Tester"


class SchedulerPageTests(unittest.TestCase):
    def setUp(self):
        worker_module.shutdown_worker()
        self.app = make_app()
        self.client = self.app.test_client()
        patcher = mock.patch("modules.admin.log_action")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = None

    def tearDown(self):
        worker_module._pool = None
        if self.pool is not None:
            self.pool.gate.set()
            self.pool.stop(timeout=2)

    def install_pool(self, algorithm="fcfs"):
        self.pool = FakePool({**MIXED, BLOCKER: meta(-5)}, pool_size=1,
                             scheduling_algorithm=algorithm)
        self.pool.start()
        worker_module._pool = self.pool
        return self.pool

    def backlog(self):
        self.pool.enqueue(BLOCKER)
        self.assertTrue(wait_until(lambda: BLOCKER in self.pool.started))
        for i in ARRIVAL:
            self.pool.enqueue(i)

    # ---- access control -------------------------------------------------

    def test_requires_login(self):
        r = self.client.get("/admin/scheduler")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_non_admin_cannot_view_or_change(self):
        self.install_pool()
        login(self.client, "recruiter")
        self.assertNotEqual(self.client.get("/admin/scheduler").status_code, 200)
        r = self.client.post("/admin/scheduler/algorithm", data={"algorithm": "priority"})
        self.assertNotEqual(r.status_code, 200)
        self.assertEqual(self.pool.get_scheduling_algorithm(), "fcfs")

    # ---- page -------------------------------------------------------------

    def test_page_without_worker_says_not_running(self):
        login(self.client)
        r = self.client.get("/admin/scheduler")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"not running", r.data)

    def test_page_shows_queue_positions_in_priority_order(self):
        self.install_pool("priority")
        self.backlog()
        login(self.client)
        html = self.client.get("/admin/scheduler").get_data(as_text=True)
        self.assertIn("Priority (earliest deadline first)", html)
        rows = re.findall(r"<tr>\s*<td>(\d+)</td>\s*<td>#(\d+)</td>", html)
        waiting = [(int(p), int(a)) for p, a in rows][:8]   # waiting table comes first
        self.assertEqual([a for _, a in waiting], [4, 5, 2, 6, 1, 7, 8, 3])
        self.assertEqual([p for p, _ in waiting], list(range(1, 9)))
        self.assertIn("upcoming", html)
        self.assertIn("expired", html)
        self.assertIn("no deadline", html)

    def test_page_shows_fcfs_order(self):
        self.install_pool("fcfs")
        self.backlog()
        login(self.client)
        html = self.client.get("/admin/scheduler").get_data(as_text=True)
        rows = re.findall(r"<tr>\s*<td>(\d+)</td>\s*<td>#(\d+)</td>", html)[:8]
        self.assertEqual([int(a) for _, a in rows], [8, 1, 2, 3, 4, 5, 6, 7])

    # ---- switching ---------------------------------------------------------

    def test_post_switches_algorithm_and_page_reorders(self):
        self.install_pool("fcfs")
        self.backlog()
        login(self.client)
        r = self.client.post("/admin/scheduler/algorithm", data={"algorithm": "priority"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.pool.get_scheduling_algorithm(), "priority")
        self.log.assert_called_once()
        self.assertEqual(self.log.call_args[0][1], "admin_set_scheduling_algorithm")
        html = self.client.get("/admin/scheduler").get_data(as_text=True)
        rows = re.findall(r"<tr>\s*<td>(\d+)</td>\s*<td>#(\d+)</td>", html)[:8]
        self.assertEqual([int(a) for _, a in rows], [4, 5, 2, 6, 1, 7, 8, 3])

    def test_switching_to_same_algorithm_is_not_audited(self):
        self.install_pool("fcfs")
        login(self.client)
        self.client.post("/admin/scheduler/algorithm", data={"algorithm": "fcfs"})
        self.log.assert_not_called()

    def test_invalid_algorithm_is_rejected_and_unchanged(self):
        self.install_pool("fcfs")
        login(self.client)
        r = self.client.post("/admin/scheduler/algorithm", data={"algorithm": "lottery"},
                             follow_redirects=True)
        self.assertEqual(self.pool.get_scheduling_algorithm(), "fcfs")
        self.assertIn(b"Unknown scheduling algorithm", r.data)
        self.log.assert_not_called()

    def test_switch_without_worker_reports_error(self):
        login(self.client)
        r = self.client.post("/admin/scheduler/algorithm", data={"algorithm": "priority"},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"worker is not running", r.data)

    def test_dispatched_table_shows_sequence_and_worker(self):
        self.install_pool("fcfs")
        self.pool.gate.set()
        for i in (1, 2, 3):
            self.pool.enqueue(i)
        self.assertTrue(wait_until(lambda: self.pool.get_stats()["completed"] == 3))
        login(self.client)
        html = self.client.get("/admin/scheduler").get_data(as_text=True)
        self.assertIn("recroot-worker-1", html)
        self.assertIn("Dispatch #", html)
        self.assertIn("Completion #", html)

    def test_nav_link_present_for_admin(self):
        login(self.client)
        self.assertIn(b"/admin/scheduler", self.client.get("/admin/scheduler").data)


class MonitorPolicyTests(unittest.TestCase):
    def test_monitor_reports_live_algorithm(self):
        view = build_monitor_view(True, {"running": True, "scheduling_algorithm": "priority"})
        self.assertEqual(view["policy"], "Priority (deadline)")
        view = build_monitor_view(True, {"running": True, "scheduling_algorithm": "fcfs"})
        self.assertEqual(view["policy"], "FCFS")

    def test_monitor_falls_back_when_worker_reports_nothing(self):
        view = build_monitor_view(False, {"running": False})
        self.assertEqual(view["policy"], "FIFO")


if __name__ == "__main__":
    unittest.main()
