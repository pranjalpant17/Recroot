"""
Tests for the "Run Real Deadlock Test" feature.

IMPORTANT - WHAT THESE TESTS DO AND DO NOT PROVE
    They run the feature against tests/fake_innodb.py, an in-memory MODEL of
    InnoDB row locking. That verifies the orchestration: two connections and
    threads, the lock-order inversion, retry through db_retry, rollback, exact
    once-only effects, cleanup, table-ownership safety, timeouts, the admin
    route and the command line. It does NOT prove that a real MySQL server
    deadlocks and raises error 1213; only running the feature against a real
    server does that:

        python -m modules.real_deadlock_test --yes

Nothing here connects to a database; every connection is the fake.

Run from the project root:   python -m unittest tests.test_real_deadlock -v
"""

import io
import os
import re
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_deadlock_handling import (  # noqa: E402  (installs stand-ins first)
    FakeAdminDB, OperationalError, _make_app, deadlock, lock_timeout,
)
from tests.fake_innodb import FakeMySQLServer  # noqa: E402
from modules import deadlock_monitor, real_deadlock_test as rdt  # noqa: E402
from modules.db_retry import lock_events  # noqa: E402

CFG = {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "recroot_FAKE"}
FAST = dict(lock_wait_seconds=2, base_delay=0.01, max_delay=0.05)

# every table of the real Recroot schema (read from schema.sql in the project)
def read_text(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


SCHEMA_TABLES = set(re.findall(r"CREATE TABLE (\w+)", read_text("schema.sql")))


def run(server, **overrides):
    kwargs = dict(FAST)
    kwargs.update(overrides)
    return rdt.run_real_deadlock_test(CFG, connect=server.connect, **kwargs)


def check_status(result, fragment):
    matches = [c["status"] for c in result["checks"] if fragment in c["label"]]
    assert matches, f"no check containing {fragment!r}"
    return matches[0]


def demo_threads():
    return [t.name for t in threading.enumerate() if t.name.startswith("real-deadlock-")]


# ===========================================================================
# 1. Orchestration: the deadlock, rollback, retry and recovery
# ===========================================================================

class DeadlockScenarioTests(unittest.TestCase):

    def assert_full_recovery(self, server, result):
        self.assertEqual(result["outcome"], "verified", result["summary"])
        self.assertTrue(result["real_mysql"])
        self.assertFalse(result["simulated"])
        self.assertTrue(all(c["status"] in ("pass", "n/a") for c in result["checks"]), result["checks"])

        # exactly one transaction was the victim, and it is the one that retried
        victims = [t for t in result["threads"] if t["first_error"]]
        survivors = [t for t in result["threads"] if not t["first_error"]]
        self.assertEqual((len(victims), len(survivors)), (1, 1))
        self.assertEqual(victims[0]["first_error"]["code"], 1213)
        self.assertIn("Deadlock found", victims[0]["first_error"]["message"])
        self.assertEqual((victims[0]["attempts"], survivors[0]["attempts"]), (2, 1))
        self.assertTrue(victims[0]["committed"] and survivors[0]["committed"])

        # db_retry's counters, private to this test
        c = result["test_counters"]
        self.assertEqual((c["deadlocks"], c["lock_timeouts"], c["retries"], c["rollbacks"],
                          c["rollback_failures"], c["recovered"], c["exhausted"]), (1, 0, 1, 1, 0, 1, 0))

        # each row changed exactly once per transaction (the victim's first try was undone)
        self.assertEqual([r["counter"] for r in result["final_rows"]], [2, 2])

        # cleanup verified; the test table is kept but holds none of this run's rows
        self.assertEqual((result["cleanup"]["ok"], result["cleanup"]["remaining"],
                          result["cleanup"]["deleted"]), (True, 0, 2))
        self.assertEqual(server.tables[rdt.TEST_TABLE]["rows"], {})
        self.assertTrue(all(conn.closed for conn in server.connections))

    def test_victim_is_the_requester_that_closed_the_cycle(self):
        server = FakeMySQLServer(victim="requester")
        self.assert_full_recovery(server, run(server))

    def test_victim_is_the_other_waiting_transaction(self):
        server = FakeMySQLServer(victim="other")
        self.assert_full_recovery(server, run(server))

    def test_repeated_runs_are_stable_and_leak_no_threads(self):
        for i in range(20):
            server = FakeMySQLServer(victim="requester" if i % 2 else "other")
            self.assert_full_recovery(server, run(server))
        self.assertEqual(demo_threads(), [])

    def test_the_victim_really_issues_an_explicit_rollback_on_its_own_connection(self):
        server = FakeMySQLServer()
        result = run(server)
        victim = [t for t in result["threads"] if t["first_error"]][0]
        survivor = [t for t in result["threads"] if not t["first_error"]][0]
        index = {"Transaction 1": 1, "Transaction 2": 2}                  # connections[0] is the setup one
        self.assertGreaterEqual(server.connections[index[victim["name"]]].rollback_calls, 1)
        self.assertEqual(server.connections[index[survivor["name"]]].rollback_calls, 0)

    def test_two_separate_connections_run_the_transactions_concurrently(self):
        server = FakeMySQLServer()
        result = run(server)
        updaters = {cid for cid, sql in server.statements if sql.startswith("UPDATE")}
        self.assertEqual(len(updaters), 2)                        # two distinct connections
        setup_id = min(c.id for c in server.connections)
        self.assertNotIn(setup_id, updaters)                      # not the setup connection
        meeting = [e for e in result["events"] if "both transactions hold their first lock" in e["message"]]
        self.assertEqual(len(meeting), 2)                         # each held a lock before either asked

    def test_events_come_from_db_retry_and_are_in_the_test_events_only(self):
        server = FakeMySQLServer()
        before = lock_events.snapshot()
        result = run(server)
        kinds = [(e["type"], e["kind"]) for e in reversed(result["test_events"])]
        self.assertEqual(kinds, [("error", "deadlock"), ("recovered", "recovered")])
        self.assertIn("rolled back", result["test_events"][-1]["action"])
        self.assertIn("retry in", result["test_events"][-1]["action"])
        self.assertEqual(lock_events.snapshot(), before)          # worker counters untouched

    def test_deadlock_report_is_attached_and_identified_as_this_tests(self):
        server = FakeMySQLServer()
        result = run(server)
        self.assertTrue(result["innodb_report_is_this_test"])
        self.assertIn(rdt.TEST_TABLE, result["innodb_report"])
        self.assertNotIn("TRANSACTIONS\n", result["innodb_report"])    # section ends before the next one
        self.assertEqual(result["server"]["lock_deadlocks_metric_delta"], 1)

    def test_unreadable_deadlock_report_is_not_a_failure(self):
        server = FakeMySQLServer(process_privilege=False, metrics_enabled=False)
        result = run(server)
        self.assertEqual(result["outcome"], "verified")
        self.assertIsNone(result["innodb_report"])
        self.assertIn("PROCESS", result["innodb_report_note"])
        self.assertEqual(check_status(result, "own deadlock report"), "n/a")
        self.assertNotIn("lock_deadlocks_metric_delta", result["server"])


class NoDeadlockTests(unittest.TestCase):

    def test_detection_off_reports_a_timeout_not_a_deadlock(self):
        server = FakeMySQLServer(deadlock_detect=False)
        started = time.monotonic()
        result = run(server, lock_wait_seconds=1)
        self.assertLess(time.monotonic() - started, 8)               # bounded, cannot hang
        self.assertEqual(result["outcome"], "not_reproduced")
        self.assertIn("1205", result["summary"])
        self.assertIn("innodb_deadlock_detect", result["summary"])
        self.assertEqual(result["test_counters"]["deadlocks"], 0)
        self.assertEqual(result["test_counters"]["lock_timeouts"], 1)
        self.assertEqual(check_status(result, "raised error 1213"), "fail")
        self.assertEqual(result["cleanup"]["ok"], True)
        self.assertEqual(server.tables[rdt.TEST_TABLE]["rows"], {})

    def test_a_real_1205_is_never_presented_as_verified(self):
        server = FakeMySQLServer(deadlock_detect=False)
        self.assertNotEqual(run(server, lock_wait_seconds=1)["outcome"], "verified")


# ===========================================================================
# 2. Table safety and cleanup
# ===========================================================================

class TableSafetyTests(unittest.TestCase):

    def foreign_table(self, server, comment="someone else's table", engine="InnoDB"):
        server.tables[rdt.TEST_TABLE] = {"comment": comment, "engine": engine,
                                         "rows": {1: {"id": 1, "run_id": "theirs", "label": "x", "counter": 7}},
                                         "next_id": 2}

    def test_a_foreign_table_with_the_same_name_is_refused_and_untouched(self):
        server = FakeMySQLServer()
        self.foreign_table(server)
        result = run(server)
        self.assertEqual(result["outcome"], "error")
        self.assertIn("already exists but is not a Recroot", result["error"])
        writes = [sql for _, sql in server.statements
                  if re.match(r"(INSERT|UPDATE|DELETE|CREATE)", sql)]
        self.assertEqual(writes, [])                               # not one write statement
        self.assertEqual(server.tables[rdt.TEST_TABLE]["rows"][1]["counter"], 7)
        self.assertEqual(result["cleanup"]["table_kept"], False)   # we did not create or keep anything
        self.assertTrue(result["cleanup"]["skipped"])
        self.assertEqual(result["checks"], [])                     # the test never ran: no misleading PASS/FAIL
        self.assertTrue(all(c.closed for c in server.connections))

    def test_marker_present_but_wrong_engine_is_refused(self):
        server = FakeMySQLServer()
        self.foreign_table(server, comment=rdt.TABLE_MARKER + ": ...", engine="MyISAM")
        self.assertEqual(run(server)["outcome"], "error")
        self.assertEqual(len(server.tables[rdt.TEST_TABLE]["rows"]), 1)

    def test_existing_marked_table_is_reused_and_other_runs_rows_are_left_alone(self):
        server = FakeMySQLServer()
        run(server)                                                # creates the table
        server.tables[rdt.TEST_TABLE]["rows"][99] = {"id": 99, "run_id": "other-run", "label": "keep", "counter": 5}
        result = run(server)
        self.assertEqual(result["outcome"], "verified")
        self.assertFalse(result["table_created_now"])
        self.assertEqual(list(server.tables[rdt.TEST_TABLE]["rows"]), [99])   # only this run's rows removed

    def test_table_is_created_with_the_marker_and_innodb(self):
        server = FakeMySQLServer()
        run(server)
        table = server.tables[rdt.TEST_TABLE]
        self.assertTrue(table["comment"].startswith(rdt.TABLE_MARKER))
        self.assertEqual(table["engine"], "InnoDB")

    def test_cleanup_runs_when_the_second_test_connection_cannot_be_opened(self):
        server = FakeMySQLServer()
        server.fail_connect_numbers = {3}                          # setup=1, txn1=2, txn2=3 fails
        result = run(server)
        self.assertEqual(result["outcome"], "error")
        self.assertIn("Could not open the two test connections", result["error"])
        self.assertEqual(server.tables[rdt.TEST_TABLE]["rows"], {})
        self.assertEqual(result["cleanup"]["ok"], True)
        self.assertTrue(all(c.closed for c in server.connections))

    def test_cleanup_runs_after_a_non_lock_failure_inside_a_transaction(self):
        server = FakeMySQLServer()
        server.fail_next_updates = [OperationalError(2013, "Lost connection to MySQL server during query")]
        started = time.monotonic()
        result = run(server)
        self.assertLess(time.monotonic() - started, 3)             # partner released at once, no 10 s barrier wait
        self.assertEqual(result["outcome"], "error")
        self.assertIn("2013", result["error"] + str(result["threads"]))
        self.assertEqual(server.tables[rdt.TEST_TABLE]["rows"], {})
        self.assertTrue(all(c.closed for c in server.connections))
        self.assertEqual(demo_threads(), [])

    def test_unreachable_database_is_reported_not_raised(self):
        server = FakeMySQLServer()
        server.fail_connect_numbers = {1}
        result = run(server)
        self.assertEqual(result["outcome"], "error")
        self.assertIn("Could not connect to MySQL", result["error"])
        self.assertEqual(server.statements, [])
        self.assertEqual(result["cleanup"]["ok"], True)

    def test_a_hung_transaction_is_cut_off_at_the_deadline_and_still_cleaned_up(self):
        server = FakeMySQLServer()
        server.hang_updates = True
        self.addCleanup(server.hang_event.set)
        started = time.monotonic()
        result = run(server, deadline_seconds=0.6, barrier_seconds=0.5)
        self.assertLess(time.monotonic() - started, 4)             # did not wait out the hang
        self.assertEqual(result["outcome"], "error")
        self.assertIn("did not finish within", result["error"])
        self.assertEqual(server.tables[rdt.TEST_TABLE]["rows"], {})
        self.assertEqual(result["cleanup"]["ok"], True)
        self.assertTrue(all(c.closed for c in server.connections))
        server.hang_event.set()
        deadline = time.monotonic() + 3
        while demo_threads() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(demo_threads(), [])

    def test_only_one_real_test_runs_at_a_time(self):
        server = FakeMySQLServer()
        self.assertTrue(rdt._run_gate.acquire(blocking=False))
        try:
            result = run(server)
        finally:
            rdt._run_gate.release()
        self.assertEqual(result["outcome"], "busy")
        self.assertEqual(server.connections, [])                   # nothing was opened
        self.assertEqual(run(server)["outcome"], "verified")       # and the gate was released


class SqlAuditTests(unittest.TestCase):
    """Every statement the feature sends is recorded by the model, which also
    rejects any statement that is not on its reviewed list."""

    FORBIDDEN = re.compile(r"\b(DROP|TRUNCATE|ALTER|RENAME|GRANT)\b", re.I)
    ALLOWED_START = ("CREATE TABLE IF NOT EXISTS", "SELECT TABLE_COMMENT", "SELECT VERSION", "SELECT @@",
                     "SELECT `COUNT`", "SHOW ENGINE INNODB STATUS", "SET SESSION innodb_lock_wait_timeout",
                     "DELETE FROM", "INSERT INTO", "UPDATE", "SELECT id, label, counter", "SELECT COUNT(*)")

    def statements(self, server):
        return [sql for _, sql in server.statements]

    def test_only_the_dedicated_table_is_ever_referenced(self):
        for kwargs in ({}, {"victim": "other"}, {"deadlock_detect": False}):
            server = FakeMySQLServer(**kwargs)
            run(server, lock_wait_seconds=1)
            for sql in self.statements(server):
                self.assertTrue(sql.startswith(self.ALLOWED_START), sql)
                self.assertIsNone(self.FORBIDDEN.search(sql), sql)
                if "information_schema" in sql or sql.startswith(("SELECT VERSION", "SELECT @@",
                                                                  "SHOW ENGINE", "SET SESSION")):
                    continue
                self.assertIn(rdt.TEST_TABLE, sql, sql)
                for name in SCHEMA_TABLES:
                    self.assertNotRegex(sql, rf"\b{name}\b", f"touches Recroot table {name}: {sql}")

    def test_every_delete_is_scoped_to_this_run_or_to_old_test_rows(self):
        server = FakeMySQLServer()
        result = run(server)
        deletes = [s for s in self.statements(server) if s.startswith("DELETE")]
        self.assertEqual(len(deletes), 2)
        self.assertIn("created_at < NOW() - INTERVAL 1 DAY", deletes[0])
        self.assertIn(f"run_id = '{result['run_id']}'", deletes[1])
        self.assertTrue(all(rdt.TEST_TABLE in d for d in deletes))

    def test_no_destructive_ddl_exists_in_the_module_source(self):
        source = read_text("modules", "real_deadlock_test.py")
        self.assertIsNone(re.search(r"\b(DROP|TRUNCATE|ALTER)\s+(TABLE|DATABASE|SCHEMA)", source, re.I))
        self.assertNotIn("init-db", source.replace("flask init-db", ""))

    def test_test_table_is_not_part_of_the_recroot_schema(self):
        self.assertNotIn(rdt.TEST_TABLE, SCHEMA_TABLES)
        schema = read_text("schema.sql")
        self.assertNotIn(rdt.TEST_TABLE, schema)
        self.assertRegex(rdt.TEST_TABLE, r"^[a-z0-9_]+$")

    def test_connection_settings_are_bounded(self):
        seen = []
        server = FakeMySQLServer()

        def spy(**kwargs):
            seen.append(kwargs)
            return server.connect(**kwargs)

        rdt.run_real_deadlock_test(CFG, connect=spy, **FAST)
        self.assertEqual(len(seen), 3)
        for kwargs in seen:
            self.assertIn("innodb_lock_wait_timeout = 2", kwargs["init_command"])
            for key in ("connect_timeout", "read_timeout", "write_timeout"):
                self.assertGreater(kwargs[key], 0)
            self.assertEqual(kwargs["database"], "recroot_FAKE")
        self.assertEqual([k["autocommit"] for k in seen], [True, False, False])


# ===========================================================================
# 3. Pieces: report extraction, verdict
# ===========================================================================

SAMPLE_STATUS = """=====================================
2026-10-29 10:00:00 INNODB MONITOR OUTPUT
=====================================
------------------------
LATEST DETECTED DEADLOCK
------------------------
2026-10-29 09:59:12 140123456
*** (1) TRANSACTION:
TRANSACTION 5001, ACTIVE 1 sec starting index read
UPDATE zz_recroot_deadlock_test SET counter = counter + 1 WHERE id = 41
*** (2) TRANSACTION:
TRANSACTION 5002, ACTIVE 1 sec starting index read
UPDATE zz_recroot_deadlock_test SET counter = counter + 1 WHERE id = 40
*** WE ROLL BACK TRANSACTION (2)
------------
TRANSACTIONS
------------
Trx id counter 5100
"""


class ReportAndVerdictTests(unittest.TestCase):

    def attach(self, status_text, ids):
        class Setup:
            def cursor(self_inner):
                outer = self

                class C:
                    def __enter__(s): return s
                    def __exit__(s, *a): return False
                    def execute(s, sql, params=None): s.sql = sql
                    def fetchone(s): return {"Status": status_text}
                return C()
        result = {"innodb_report": None, "innodb_report_is_this_test": None, "innodb_report_note": ""}
        rdt._attach_innodb_report(result, Setup(), ids)
        return result

    def test_extraction_stops_at_the_next_section(self):
        section = rdt._extract_deadlock_report(SAMPLE_STATUS)
        self.assertTrue(section.startswith("LATEST DETECTED DEADLOCK"))
        self.assertIn("WE ROLL BACK TRANSACTION (2)", section)
        self.assertNotIn("Trx id counter", section)

    def test_report_for_this_test_is_recognised(self):
        result = self.attach(SAMPLE_STATUS, {"row_1": 40, "row_2": 41})
        self.assertTrue(result["innodb_report_is_this_test"])

    def test_report_about_some_other_deadlock_is_not_claimed(self):
        other = SAMPLE_STATUS.replace("zz_recroot_deadlock_test", "applications")
        result = self.attach(other, {"row_1": 40, "row_2": 41})
        self.assertFalse(result["innodb_report_is_this_test"])
        self.assertIn("NOT this test's", result["innodb_report_note"])
        wrong_ids = self.attach(SAMPLE_STATUS, {"row_1": 900, "row_2": 901})
        self.assertFalse(wrong_ids["innodb_report_is_this_test"])

    def test_status_without_a_deadlock_section(self):
        result = self.attach("nothing to see here", {"row_1": 1, "row_2": 2})
        self.assertFalse(result["innodb_report_is_this_test"])
        self.assertIsNone(result["innodb_report"])

    def base_result(self, **changes):
        result = {
            "test_counters": {"deadlocks": 1, "lock_timeouts": 0, "retries": 1, "rollbacks": 1,
                              "rollback_failures": 0, "recovered": 1, "exhausted": 0},
            "threads": [{"name": "T1", "committed": True, "attempts": 2, "first_error": {"code": 1213, "message": "m"}, "error": None},
                        {"name": "T2", "committed": True, "attempts": 1, "first_error": None, "error": None}],
            "final_rows": [{"id": 1, "label": "row_1", "counter": 2}, {"id": 2, "label": "row_2", "counter": 2}],
            "cleanup": {"ok": True, "remaining": 0, "deleted": 2, "table_kept": True, "error": None},
            "innodb_report": None, "innodb_report_is_this_test": None, "error": None, "outcome": "error", "summary": "",
        }
        result.update(changes)
        return result

    def test_verdict_verified_only_when_every_required_check_passes(self):
        result = self.base_result()
        rdt._judge(result)
        self.assertEqual(result["outcome"], "verified")

    def test_each_failure_prevents_verified(self):
        cases = {
            "no 1213": dict(test_counters={"deadlocks": 0, "lock_timeouts": 0, "retries": 0, "rollbacks": 0,
                                           "rollback_failures": 0, "recovered": 0, "exhausted": 0}),
            "rollback failed": dict(test_counters={"deadlocks": 1, "lock_timeouts": 0, "retries": 1, "rollbacks": 0,
                                                   "rollback_failures": 1, "recovered": 1, "exhausted": 0}),
            "double applied": dict(final_rows=[{"id": 1, "label": "row_1", "counter": 3},
                                               {"id": 2, "label": "row_2", "counter": 2}]),
            "lost update": dict(final_rows=[{"id": 1, "label": "row_1", "counter": 1},
                                            {"id": 2, "label": "row_2", "counter": 2}]),
            "victim never recovered": dict(threads=[
                {"name": "T1", "committed": False, "attempts": 3, "first_error": {"code": 1213, "message": "m"}, "error": "x"},
                {"name": "T2", "committed": True, "attempts": 1, "first_error": None, "error": None}]),
            "cleanup failed": dict(cleanup={"ok": False, "remaining": 2, "deleted": 0, "table_kept": True, "error": "e"}),
        }
        for name, changes in cases.items():
            result = self.base_result(**changes)
            rdt._judge(result)
            self.assertNotEqual(result["outcome"], "verified", name)

    def test_informational_checks_never_block_or_grant_success(self):
        for report in (None, False):
            result = self.base_result(innodb_report_is_this_test=report)
            rdt._judge(result)
            self.assertEqual(result["outcome"], "verified")
        broken = self.base_result(final_rows=[{"id": 1, "label": "row_1", "counter": 9}] * 2,
                                  innodb_report="x", innodb_report_is_this_test=True)
        rdt._judge(broken)
        self.assertNotEqual(broken["outcome"], "verified")           # a matching report can't rescue it

    def test_deadlock_then_failure_is_partial_not_error(self):
        result = self.base_result(cleanup={"ok": False, "remaining": 2, "deleted": 0, "table_kept": True, "error": "e"})
        rdt._judge(result)
        self.assertEqual(result["outcome"], "partial")


# ===========================================================================
# 4. View-model (deadlock_monitor)
# ===========================================================================

def finished_result(**server_kwargs):
    server = FakeMySQLServer(**server_kwargs)
    return run(server)


class RealTestViewTests(unittest.TestCase):

    def setUp(self):
        deadlock_monitor.remember_real_test(None)
        self.addCleanup(deadlock_monitor.remember_real_test, None)

    def test_no_result_means_no_view(self):
        self.assertIsNone(deadlock_monitor.build_real_test_view(None))
        self.assertIsNone(deadlock_monitor.get_real_test_view())

    def test_roles_labels_and_event_rows(self):
        view = deadlock_monitor.build_real_test_view(finished_result())
        self.assertTrue(view["outcome_label"].startswith("Verified"))
        roles = sorted(t["role"].split(":")[0] for t in view["threads"])
        self.assertEqual(roles, ["survivor", "victim"])
        victim = [t for t in view["threads"] if t["role"].startswith("victim")][0]
        self.assertIn("MySQL error 1213", victim["role"])
        self.assertIn("retried and committed", victim["role"])
        rows = view["test_event_rows"]
        self.assertEqual([r["kind_label"] for r in reversed(rows)], ["Deadlock (MySQL 1213)", "Recovered"])
        self.assertTrue(rows[-1]["where"].startswith("Transaction"))          # reuses worker-event formatting
        self.assertEqual({c["mark"] for c in view["checks"]}, {"PASS"})

    def test_failed_checks_are_marked(self):
        view = deadlock_monitor.build_real_test_view(finished_result(deadlock_detect=False))
        self.assertEqual(view["outcome"], "not_reproduced")
        failed = [c["label"] for c in view["checks"] if c["mark"] == "FAIL"]
        self.assertTrue(any("raised error 1213" in label for label in failed), failed)

    def test_remembered_result_is_a_private_copy(self):
        result = finished_result()
        deadlock_monitor.remember_real_test(result)
        result["outcome"] = "tampered"
        first = deadlock_monitor.get_real_test_view()
        self.assertEqual(first["outcome"], "verified")
        first["checks"].clear()
        self.assertTrue(deadlock_monitor.get_real_test_view()["checks"])

    def test_real_test_never_feeds_the_worker_dashboard(self):
        deadlock_monitor.remember_real_test(finished_result())
        view = deadlock_monitor.get_dashboard_view()
        self.assertEqual(view["real"]["deadlocks"], 0)
        self.assertEqual(view["events"], [])
        self.assertEqual(view["health"]["state"], "quiet")


# ===========================================================================
# 5. The admin route and page
# ===========================================================================

class AdminRealTestTests(unittest.TestCase):

    def setUp(self):
        lock_events.reset()
        deadlock_monitor.remember_real_test(None)
        self.addCleanup(lock_events.reset)
        self.addCleanup(deadlock_monitor.remember_real_test, None)
        from modules import admin
        self.admin = admin
        self.server = FakeMySQLServer()
        self.calls = []

        def runner(cfg):
            self.calls.append(cfg)
            return rdt.run_real_deadlock_test(cfg, connect=self.server.connect, **FAST)

        self.runner = mock.Mock(side_effect=runner)
        self.log = mock.Mock()
        self.db = FakeAdminDB()
        mock.patch.object(admin, "run_real_deadlock_test", self.runner).start()
        mock.patch.object(admin, "log_action", self.log).start()
        mock.patch.object(admin, "_open_db", mock.Mock(return_value=self.db)).start()
        self.addCleanup(mock.patch.stopall)
        app = _make_app()
        app.config["DB_CONFIG"] = dict(CFG)
        self.client = app.test_client()
        self.login("admin")

    def login(self, role):
        with self.client.session_transaction() as s:
            s.update(user_id=7, role=role, name="Ada", email="a@x")

    def post(self):
        return self.client.post("/admin/deadlock/real-test")

    def test_only_admins_can_run_it(self):
        anonymous = _make_app().test_client()
        r = anonymous.post("/admin/deadlock/real-test")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        for role in ("candidate", "recruiter"):
            self.login(role)
            self.assertEqual(self.post().status_code, 302, role)
        self.runner.assert_not_called()
        self.log.assert_not_called()
        self.assertEqual(self.server.connections, [])

    def test_it_is_post_only(self):
        self.assertEqual(self.client.get("/admin/deadlock/real-test").status_code, 405)
        self.runner.assert_not_called()

    def test_page_offers_the_button_with_a_confirmation_before_any_run(self):
        html = self.client.get("/admin/deadlock").get_data(as_text=True)
        self.assertIn("Run Real Deadlock Test", html)
        self.assertIn("/admin/deadlock/real-test", html)
        self.assertIn("No real deadlock test has been run in this server process yet.", html)
        self.assertIn("confirm(", html)
        self.assertIn("No Recroot data is touched", html)
        self.assertIn('name="csrf_token"', html)
        self.runner.assert_not_called()

    def test_running_it_shows_the_real_test_results(self):
        r = self.post()
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        for text in ("Verified: genuine MySQL deadlock", "REAL TEST", "id=\"real-test-result\"",
                     "Deadlock found when trying to get lock", "Deadlock (MySQL 1213)", "Recovered",
                     "Transaction 1", "Transaction 2", "victim: MySQL error 1213", "survivor",
                     "zz_recroot_deadlock_test", "PASS", "(verified)", "kept, empty"):
            self.assertIn(text, html)
        self.assertNotIn("FAIL", html.split('id="real-test-result"')[1].split('id="simulation"')[0])

    def test_it_receives_the_apps_own_db_config(self):
        self.post()
        self.assertEqual(self.calls, [CFG])

    def test_real_test_is_separate_from_worker_counters_and_from_the_simulation(self):
        self.post()
        html = self.client.get("/admin/deadlock").get_data(as_text=True)
        before_sim, sim = html.split('id="simulation"')
        before_test, test_part = before_sim.split('id="real-test"')
        # worker counters show nothing: the test's deadlock is not counted there
        for label in ("Deadlocks", "Rollbacks", "Successful recoveries"):
            m = re.search(r'stat-value">\s*(\d+)\s*</div>\s*<div class="stat-label">\s*' + label, before_test)
            self.assertEqual(m.group(1), "0", label)
        self.assertIn("No lock events recorded", before_test)
        self.assertIn("No MySQL deadlock or lock wait timeout", before_test)
        # the test results live only inside the real-test section
        self.assertIn('id="real-test-result"', test_part)
        self.assertNotIn("real-test-result", before_test)
        self.assertNotIn("real-test-result", sim)
        # and the simulation section stays labelled as such, without real-test content
        self.assertIn("SIMULATION ONLY", sim)
        self.assertNotIn("zz_recroot_deadlock_test", sim)
        self.assertEqual(lock_events.snapshot()["total_mysql_events"], 0)

    def test_sections_are_in_order(self):
        html = self.client.get("/admin/deadlock").get_data(as_text=True)
        order = [html.index(m) for m in ('id="real-events"', 'id="persisted"', 'id="real-test"', 'id="simulation"')]
        self.assertEqual(order, sorted(order))

    def test_result_survives_page_refresh_and_refresh_is_not_offered_after_the_post(self):
        posted = self.post().get_data(as_text=True)
        self.assertNotIn('id="auto-refresh"', posted)
        self.assertNotIn("window.location.reload", posted)
        later = self.client.get("/admin/deadlock").get_data(as_text=True)
        self.assertIn('id="real-test-result"', later)                # still there
        self.assertIn('id="auto-refresh"', later)                    # refresh is safe again on a GET
        self.assertEqual(self.runner.call_count, 1)                  # the GET did not re-run anything

    def test_an_audit_entry_is_written_through_the_existing_helper(self):
        self.post()
        self.log.assert_called_once()
        user_id, action, details = self.log.call_args.args
        self.assertEqual((user_id, action), (7, "admin_real_deadlock_test"))
        self.assertIn("zz_recroot_deadlock_test", details)
        self.assertIn("verified", details)

    def test_audit_failure_does_not_break_the_page(self):
        self.log.side_effect = RuntimeError("audit down")
        r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Verified: genuine MySQL deadlock", r.get_data(as_text=True))

    def test_unexpected_runner_exception_still_gives_a_page(self):
        self.runner.side_effect = RuntimeError("boom")
        r = self.post()
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Test could not run", html)
        self.assertIn("RuntimeError", html)

    def test_busy_result_is_shown_but_neither_remembered_nor_audited(self):
        self.runner.side_effect = None
        self.runner.return_value = {"kind": "real_deadlock_test", "outcome": "busy", "table": rdt.TEST_TABLE,
                                    "summary": "A real deadlock test is already running. Try again in a moment."}
        html = self.post().get_data(as_text=True)
        self.assertIn("Not run: another real deadlock test is in progress", html)
        self.log.assert_not_called()
        self.assertIsNone(deadlock_monitor.get_real_test_view())

    def test_refused_foreign_table_is_explained_without_claiming_a_table_was_kept(self):
        self.server.tables[rdt.TEST_TABLE] = {"comment": "foreign", "engine": "InnoDB", "rows": {}, "next_id": 1}
        html = self.post().get_data(as_text=True)
        self.assertIn("Test could not run", html)
        self.assertIn("already exists but is not a Recroot deadlock-test table", html)
        self.assertIn("No table was created or changed.", html)
        self.assertNotIn("kept, empty", html)
        self.assertNotIn(">FAIL<", html)                              # nothing ran, so no red checks
        self.assertNotIn("<h3 class=\"mt-16\">Checks</h3>", html)
        self.assertNotIn("Test rows after both transactions", html)

    def test_the_page_makes_no_candidate_or_application_database_access(self):
        self.post()
        self.client.get("/admin/deadlock")
        statements = [sql for _, sql in self.server.statements]
        for sql in statements:
            for name in SCHEMA_TABLES:
                self.assertNotRegex(sql, rf"\b{name}\b", sql)
        for sql in self.db.statements:                               # the dashboard's own two reads
            self.assertTrue(sql.startswith("SELECT"), sql)


# ===========================================================================
# 6. Command line
# ===========================================================================

class CommandLineTests(unittest.TestCase):

    def call(self, argv, server=None):
        out = io.StringIO()
        server = server or FakeMySQLServer()
        code = rdt.main(argv, connect=server.connect, out=lambda text: out.write(str(text) + "\n"))
        return code, out.getvalue(), server

    def test_without_yes_it_only_explains_and_touches_nothing(self):
        code, text, server = self.call([])
        self.assertEqual(code, 2)
        self.assertIn("--yes", text)
        self.assertIn(rdt.TEST_TABLE, text)
        self.assertEqual((server.connections, server.statements), ([], []))

    def test_with_yes_it_runs_prints_a_report_and_exits_zero_when_verified(self):
        code, text, server = self.call(["--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Outcome: VERIFIED", text)
        self.assertIn("[PASS] MySQL raised error 1213", text)
        self.assertIn("Cleanup: deleted 2 row(s), remaining 0", text)

    def test_exit_code_is_nonzero_when_no_deadlock_is_verified(self):
        code, text, _ = self.call(["--yes"], FakeMySQLServer(deadlock_detect=False))
        self.assertEqual(code, 1)
        self.assertIn("NOT_REPRODUCED", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
