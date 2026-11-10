"""
Recroot - Real MySQL deadlock test

Triggers a GENUINE InnoDB deadlock between two concurrent transactions on two
separate connections, then shows MySQL error 1213, the rollback, the retry and
the recovery. It runs against your existing `recroot` database but touches
ONLY one dedicated table, never a candidate/application/job/user table.

    python -m modules.real_deadlock_test --Ayes        (command line)
    admin dashboard -> "Run Real Deadlock Test"        (web)

HOW THE DEADLOCK IS PRODUCED (classic lock-order inversion)
    The test table gets two rows, R1 and R2 (primary-key rows; record locks only).

        Transaction 1                       Transaction 2
        UPDATE ... WHERE id = R1  (locks)   UPDATE ... WHERE id = R2  (locks)
        -- both wait here until each holds its first lock --
        UPDATE ... WHERE id = R2  (waits)   UPDATE ... WHERE id = R1  (waits)
                      ^---- circular wait: InnoDB detects it ----^

    InnoDB picks one transaction as the victim, rolls it back and raises
    error 1213 to it; the other one proceeds. Which one is chosen is MySQL's
    decision, so the code handles either. The victim's whole transaction is
    then re-run through modules.db_retry.run_with_lock_retry (rollback, backoff,
    retry from the first statement) and this time it succeeds.

WHAT COUNTS AS "GENUINE"
    Success requires an exception carrying MySQL error code 1213 that the
    SERVER raised. Nothing here fabricates, simulates or pre-fills it. If the
    server does not deadlock (for example innodb_deadlock_detect=OFF makes it
    wait for the lock-wait timeout, error 1205, instead) the outcome is
    "not_reproduced" and says so. As supporting evidence the test also reads
    MySQL's own "LATEST DETECTED DEADLOCK" report (SHOW ENGINE INNODB STATUS,
    needs the PROCESS privilege; skipped if unavailable) and checks that it
    names this test's table.

SAFETY
    * One fixed table, zz_recroot_deadlock_test, created with
      CREATE TABLE IF NOT EXISTS and stamped with a marker comment. A table of
      that name WITHOUT the marker is refused and left untouched.
    * No DROP, TRUNCATE or ALTER anywhere in this module. The table is kept
      (empty after each run); cleanup is DELETE ... WHERE run_id = <this run>
      plus removal of this table's own rows older than one day (left by a
      crashed run). Cleanup runs in `finally` and is verified by a COUNT.
    * Every statement is a fixed string with the table name as a constant; no
      user input reaches any SQL text.
    * Bounded in time: connect/read/write timeouts, innodb_lock_wait_timeout
      on both test sessions, barrier timeouts, and a hard deadline on joining
      the two threads. One run at a time per process.
    * Separate connections, never Flask's request connection, and never the
      worker's. The test uses its own LockEventCounters, so the worker's
      real counters are not touched.
    * Does not import app.py (so it cannot start the worker pool).
"""

import re
import sys
import threading
import time
import uuid
from datetime import datetime

from modules.db_retry import (
    ER_LOCK_DEADLOCK,
    ER_LOCK_WAIT_TIMEOUT,
    LockEventCounters,
    mysql_error_code,
    run_with_lock_retry,
)

__all__ = ["TEST_TABLE", "TABLE_MARKER", "run_real_deadlock_test", "format_report", "main"]

TEST_TABLE = "zz_recroot_deadlock_test"
TABLE_MARKER = "recroot-deadlock-test"
assert re.fullmatch(r"[a-z0-9_]+", TEST_TABLE)       # constant, safe to put in SQL text

_CREATE_TABLE = (
    f"CREATE TABLE IF NOT EXISTS {TEST_TABLE} ("
    " id INT AUTO_INCREMENT PRIMARY KEY,"
    " run_id VARCHAR(36) NOT NULL,"
    " label VARCHAR(20) NOT NULL,"
    " counter INT NOT NULL DEFAULT 0,"
    " created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
    " KEY idx_run (run_id)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
    f"COMMENT='{TABLE_MARKER}: scratch table used only by the admin Real Deadlock Test'"
)

DEFAULT_LOCK_WAIT_SECONDS = 5       # innodb_lock_wait_timeout for the two test sessions
DEFAULT_BARRIER_SECONDS = 10
DEFAULT_DEADLINE_SECONDS = 30       # hard cap on the whole concurrent phase
MAX_REPORT_CHARS = 6000

_run_gate = threading.Lock()        # one real test at a time in this process


# ============================================================
# Small helpers
# ============================================================

class _Timeline:
    def __init__(self):
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._events = []

    def add(self, actor, message, code=None):
        entry = {"t": round(time.monotonic() - self._start, 3), "actor": actor,
                 "message": message, "code": code}
        with self._lock:
            self._events.append(entry)

    def events(self):
        with self._lock:
            return sorted(self._events, key=lambda e: e["t"])

    def elapsed(self):
        return round(time.monotonic() - self._start, 3)


class _Abort(Exception):
    """The test itself could not proceed (not a database lock error)."""


def _default_connect(**kwargs):
    import pymysql
    return pymysql.connect(**kwargs)


def _conn_kwargs(cfg, lock_wait_seconds, autocommit, timeout_budget):
    try:
        import pymysql.cursors
        cursorclass = pymysql.cursors.DictCursor
    except ImportError:                  # only possible with an injected connect()
        cursorclass = None
    return {
        "host": cfg["host"], "port": cfg["port"], "user": cfg["user"],
        "password": cfg["password"], "database": cfg["database"],
        "charset": "utf8mb4",
        "cursorclass": cursorclass,
        "autocommit": autocommit,
        "connect_timeout": 5,
        "read_timeout": timeout_budget,
        "write_timeout": timeout_budget,
        "init_command": f"SET SESSION innodb_lock_wait_timeout = {int(lock_wait_seconds)}",
    }


def _one(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _error_text(exc):
    args = getattr(exc, "args", ())
    return str(args[1]) if len(args) > 1 else str(exc)


def _describe(exc):
    """'ErrorType (MySQL error N): message' for results shown to the admin."""
    code = mysql_error_code(exc)
    where = f" (MySQL error {code})" if code else ""
    return f"{type(exc).__name__}{where}: {_error_text(exc)}"


def _extract_deadlock_report(status_text):
    """The 'LATEST DETECTED DEADLOCK' section of SHOW ENGINE INNODB STATUS."""
    marker = "LATEST DETECTED DEADLOCK"
    start = status_text.find(marker)
    if start < 0:
        return None
    body = status_text[start + len(marker):]
    nxt = re.search(r"\n-{6,}\n[A-Z][A-Z ,&/-]+\n-{6,}\n", body[10:])
    section = body[: nxt.start() + 10] if nxt else body
    return (marker + section).strip()[:MAX_REPORT_CHARS]


# ============================================================
# Table creation (reviewed carefully: see module docstring)
# ============================================================

def _ensure_test_table(setup):
    """Create the dedicated table if needed and prove it is OURS. Returns
    (created: bool). Raises _Abort, touching nothing else, if a table with
    this name exists but is not a Recroot test table."""
    existing = _one(
        setup,
        "SELECT TABLE_COMMENT AS table_comment, ENGINE AS table_engine "
        "FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (TEST_TABLE,),
    )
    if existing is not None:
        comment = str(existing.get("table_comment") or "")
        if not comment.startswith(TABLE_MARKER) or str(existing.get("table_engine")).upper() != "INNODB":
            raise _Abort(
                f"A table named {TEST_TABLE} already exists but is not a Recroot "
                f"deadlock-test table (marker/engine mismatch). Refusing to use it; "
                f"nothing was changed.")
        return False
    with setup.cursor() as cur:
        cur.execute(_CREATE_TABLE)
    return True


# ============================================================
# The test
# ============================================================

def run_real_deadlock_test(db_config, *, connect=None, counters=None,
                           lock_wait_seconds=DEFAULT_LOCK_WAIT_SECONDS,
                           barrier_seconds=DEFAULT_BARRIER_SECONDS,
                           deadline_seconds=DEFAULT_DEADLINE_SECONDS,
                           max_retries=2, base_delay=0.2, max_delay=1.0,
                           sleep=time.sleep):
    """
    Run the real deadlock test and return a result dict (never raises).

    db_config  dict with host, port, user, password, database
    connect    pymysql.connect-compatible callable (injectable for tests)
    counters   LockEventCounters for THIS test (a fresh private one by
               default; never the worker's global counters)
    """
    result = {
        "kind": "real_deadlock_test", "real_mysql": True, "simulated": False,
        "table": TEST_TABLE, "database": str(db_config.get("database", "")),
        "started_at_text": _now_text(), "outcome": "error", "summary": "",
        "run_id": None, "error": None, "events": [], "test_events": [],
        "test_counters": {}, "threads": [], "final_rows": [], "checks": [],
        "server": {}, "innodb_report": None, "innodb_report_is_this_test": None,
        "innodb_report_note": "", "cleanup": {"ok": None, "remaining": None,
                                              "deleted": 0, "table_kept": True,
                                              "error": None},
        "table_created_now": False, "elapsed_seconds": 0.0,
    }
    if not _run_gate.acquire(blocking=False):
        result["outcome"] = "busy"
        result["summary"] = "A real deadlock test is already running. Try again in a moment."
        return result
    try:
        _run(result, db_config, connect or _default_connect,
             counters if counters is not None else LockEventCounters(),
             lock_wait_seconds, barrier_seconds, deadline_seconds,
             max_retries, base_delay, max_delay, sleep)
    finally:
        _run_gate.release()
    return result


def _run(result, cfg, connect, counters, lock_wait, barrier_seconds, deadline_seconds,
         max_retries, base_delay, max_delay, sleep):
    timeline = _Timeline()
    run_id = str(uuid.uuid4())
    result["run_id"] = run_id
    budget = int(lock_wait + deadline_seconds)
    setup = conn1 = conn2 = None
    ids = {}
    owned = False          # True only AFTER the table was proven to be ours
    try:
        # ---- 1. setup connection: table, rows, server facts (autocommit) ----
        try:
            setup = connect(**_conn_kwargs(cfg, lock_wait, True, budget))
        except Exception as exc:
            raise _Abort(f"Could not connect to MySQL: {_describe(exc)}")
        result["table_created_now"] = _ensure_test_table(setup)
        owned = True
        timeline.add("Setup", f"table {TEST_TABLE} " +
                     ("created" if result["table_created_now"] else "already present (marker verified)"))

        with setup.cursor() as cur:
            cur.execute(f"DELETE FROM {TEST_TABLE} WHERE created_at < NOW() - INTERVAL 1 DAY")
        result["server"] = _server_facts(setup)
        before = _deadlock_metric(setup)

        for label in ("row_1", "row_2"):
            with setup.cursor() as cur:
                cur.execute(f"INSERT INTO {TEST_TABLE} (run_id, label, counter) VALUES (%s, %s, 0)",
                            (run_id, label))
                ids[label] = cur.lastrowid
        timeline.add("Setup", f"inserted 2 test rows (ids {ids['row_1']} and {ids['row_2']}), counter = 0")

        # ---- 2. two separate connections for the two transactions ----
        try:
            conn1 = connect(**_conn_kwargs(cfg, lock_wait, False, budget))
            conn2 = connect(**_conn_kwargs(cfg, lock_wait, False, budget))
        except Exception as exc:
            raise _Abort(f"Could not open the two test connections: {_describe(exc)}")

        # ---- 3. the concurrent phase ----
        barrier = threading.Barrier(2, timeout=barrier_seconds)
        outcomes = {}
        specs = [
            ("Transaction 1", conn1, ids["row_1"], ids["row_2"]),
            ("Transaction 2", conn2, ids["row_2"], ids["row_1"]),
        ]

        def transaction(name, conn, first_id, second_id):
            state = {"attempts": 0, "first_error": None}

            def operation():
                state["attempts"] += 1
                attempt = state["attempts"]
                tag = f"{name}" + (f" (retry, attempt {attempt})" if attempt > 1 else "")
                conn.begin()
                _update(conn, first_id)
                timeline.add(tag, f"UPDATE row id={first_id}: lock acquired")
                if attempt == 1:
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        raise _Abort(f"{name}: the other transaction never reached the meeting point")
                    timeline.add(tag, f"both transactions hold their first lock; now requesting row id={second_id}")
                try:
                    _update(conn, second_id)
                except Exception as exc:
                    code = mysql_error_code(exc)
                    if code in (ER_LOCK_DEADLOCK, ER_LOCK_WAIT_TIMEOUT):
                        if state["first_error"] is None:
                            state["first_error"] = {"code": code, "message": _error_text(exc)}
                        timeline.add(tag, f"MySQL error {code}: {_error_text(exc)}", code=code)
                    raise
                timeline.add(tag, f"UPDATE row id={second_id}: lock acquired")
                conn.commit()
                timeline.add(tag, "COMMIT")
                return attempt

            def rollback():
                conn.rollback()
                timeline.add(name, "ROLLBACK executed on this connection")

            try:
                attempts = run_with_lock_retry(
                    operation, rollback, max_retries=max_retries, base_delay=base_delay,
                    max_delay=max_delay, jitter=0.25, sleep=sleep, counters=counters, where=name)
                outcomes[name] = {"name": name, "committed": True, "attempts": attempts,
                                  "first_error": state["first_error"], "error": None}
            except Exception as exc:
                barrier.abort()          # never leave the other transaction waiting for us
                outcomes[name] = {"name": name, "committed": False, "attempts": state["attempts"],
                                  "first_error": state["first_error"],
                                  "error": _describe(exc)}
                timeline.add(name, f"gave up: {_describe(exc)}",
                             code=mysql_error_code(exc))
                try:
                    conn.rollback()
                except Exception:
                    pass

        threads = [threading.Thread(target=transaction, args=spec, daemon=True,
                                    name=f"real-deadlock-{i + 1}") for i, spec in enumerate(specs)]
        for t in threads:
            t.start()
        end = time.monotonic() + deadline_seconds
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
        if any(t.is_alive() for t in threads):
            barrier.abort()
            raise _Abort(f"The test did not finish within {deadline_seconds:g}s and was stopped.")

        # ---- 4. evidence ----
        result["threads"] = [outcomes[s[0]] for s in specs if s[0] in outcomes]
        for t in result["threads"]:
            if t["error"] and not result["error"]:
                result["error"] = f"{t['name']}: {t['error']}"
        rows = []
        with setup.cursor() as cur:
            cur.execute(f"SELECT id, label, counter FROM {TEST_TABLE} WHERE run_id = %s ORDER BY id",
                        (run_id,))
            rows = [dict(r) for r in cur.fetchall()]
        result["final_rows"] = rows
        _attach_innodb_report(result, setup, ids)
        after = _deadlock_metric(setup)
        if before is not None and after is not None and after > before:
            result["server"]["lock_deadlocks_metric_delta"] = after - before

    except _Abort as exc:
        result["error"] = str(exc)
    except Exception as exc:                     # unexpected: report, never raise
        result["error"] = _describe(exc)
    finally:
        result["cleanup"] = _cleanup(setup, run_id, owned, conn1, conn2)
        result["events"] = timeline.events()
        result["elapsed_seconds"] = timeline.elapsed()
        snapshot = counters.snapshot()
        result["test_events"] = snapshot["events"]
        result["test_counters"] = {k: snapshot[k] for k in
                                   ("deadlocks", "lock_timeouts", "retries", "rollbacks",
                                    "rollback_failures", "recovered", "exhausted")}
        _judge(result)


def _update(conn, row_id):
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {TEST_TABLE} SET counter = counter + 1 WHERE id = %s", (row_id,))


def _server_facts(setup):
    facts = {"version": None, "deadlock_detect": None, "isolation": None}
    for key, sql in (("version", "SELECT VERSION() AS v"),
                     ("deadlock_detect", "SELECT @@innodb_deadlock_detect AS v"),
                     ("isolation", "SELECT @@transaction_isolation AS v")):
        try:
            row = _one(setup, sql)
            facts[key] = None if row is None else str(row.get("v"))
        except Exception:
            facts[key] = None
    return facts


def _deadlock_metric(setup):
    """MySQL's own cumulative lock_deadlocks counter, if readable (best effort)."""
    try:
        row = _one(setup, "SELECT `COUNT` AS metric_count FROM information_schema.INNODB_METRICS "
                          "WHERE NAME = 'lock_deadlocks'")
        return None if row is None else int(row["metric_count"])
    except Exception:
        return None


def _attach_innodb_report(result, setup, ids):
    try:
        row = _one(setup, "SHOW ENGINE INNODB STATUS")
        text = str((row or {}).get("Status") or "")
    except Exception as exc:
        result["innodb_report_note"] = (f"MySQL's own deadlock report was not readable "
                                        f"({type(exc).__name__}; it needs the PROCESS privilege).")
        return
    section = _extract_deadlock_report(text)
    if section is None:
        result["innodb_report_note"] = "MySQL's status output contains no deadlock report."
        result["innodb_report_is_this_test"] = False
        return
    mine = TEST_TABLE in section and any(f"id = {i}" in section for i in ids.values())
    result["innodb_report"] = section
    result["innodb_report_is_this_test"] = bool(mine)
    result["innodb_report_note"] = (
        "MySQL's own report of the latest detected deadlock; it names this test's table and rows."
        if mine else
        "MySQL's report of the latest detected deadlock, but it is NOT this test's "
        "(an older deadlock elsewhere).")


# ============================================================
# Cleanup (reviewed carefully: see module docstring)
# ============================================================

def _cleanup(setup, run_id, owned, conn1, conn2):
    """Close the two transaction connections (the server rolls back anything
    still open when a connection closes), then remove THIS run's rows and only
    those. Rows are deleted only if `owned`, i.e. the table was proven to be
    the Recroot test table; a refused foreign table is never written to.
    Never raises. The table itself is kept."""
    info = {"ok": None, "remaining": None, "deleted": 0, "table_kept": True, "error": None,
            "skipped": False}
    for conn in (conn1, conn2):
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if setup is None or not owned:
        info["ok"] = True                  # nothing was created, so nothing to remove
        info["remaining"] = 0
        info["skipped"] = True
        info["table_kept"] = False
        if setup is not None:
            try:
                setup.close()
            except Exception:
                pass
        return info
    try:
        with setup.cursor() as cur:
            info["deleted"] = int(cur.execute(f"DELETE FROM {TEST_TABLE} WHERE run_id = %s", (run_id,)) or 0)
        row = _one(setup, f"SELECT COUNT(*) AS n FROM {TEST_TABLE} WHERE run_id = %s", (run_id,))
        info["remaining"] = int(row["n"])
        info["ok"] = info["remaining"] == 0
    except Exception as exc:
        info["ok"] = False
        info["error"] = _describe(exc)
    finally:
        try:
            setup.close()
        except Exception:
            pass
    return info


# ============================================================
# Verdict
# ============================================================

def _judge(result):
    counters = result["test_counters"]
    threads = result["threads"]
    rows = result["final_rows"]
    deadlock_seen = bool(counters.get("deadlocks"))
    timeout_seen = bool(counters.get("lock_timeouts"))
    retried_and_committed = any(t["committed"] and t["attempts"] >= 2 for t in threads)
    both_committed = len(threads) == 2 and all(t["committed"] for t in threads)
    exactly_once = len(rows) == 2 and all(r["counter"] == 2 for r in rows)
    cleanup_ok = result["cleanup"].get("ok") is True

    ran = bool(threads) or deadlock_seen or timeout_seen
    report = result["innodb_report_is_this_test"]
    result["checks"] = [] if not ran else [
        {"label": "MySQL raised error 1213 (deadlock) to one of the two transactions",
         "status": "pass" if deadlock_seen else "fail"},
        {"label": "The victim's transaction was rolled back",
         "status": "pass" if (deadlock_seen and counters.get("rollbacks", 0) >= 1
                              and counters.get("rollback_failures", 0) == 0) else "fail"},
        {"label": "The victim retried (db_retry) and committed",
         "status": "pass" if retried_and_committed else "fail"},
        {"label": "The other transaction committed",
         "status": "pass" if both_committed else "fail"},
        {"label": "Each row changed exactly twice, once per transaction (no lost or doubled update)",
         "status": "pass" if exactly_once else "fail"},
        {"label": "MySQL's own deadlock report names this test's table",
         "status": "n/a" if report is None and not result["innodb_report"] else
                   ("pass" if report else "warn")},
        {"label": "Test rows removed and verified (table kept, empty)",
         "status": "pass" if cleanup_ok else "fail"},
    ]
    required = [c for i, c in enumerate(result["checks"]) if i != 5]
    all_required = ran and all(c["status"] == "pass" for c in required)

    if not ran:
        result["outcome"] = "error"
        result["summary"] = result["error"] or "The test did not run."
    elif result["error"] and not (deadlock_seen or timeout_seen):
        result["outcome"] = "error"
        result["summary"] = result["error"]
    elif all_required:
        result["outcome"] = "verified"
        result["summary"] = ("A genuine MySQL deadlock occurred (error 1213 raised by the server). "
                             "The victim was rolled back, retried and recovered, both transactions "
                             "committed exactly once, and the test rows were removed.")
    elif deadlock_seen:
        result["outcome"] = "partial"
        result["summary"] = ("MySQL raised a genuine deadlock (error 1213), but not every recovery "
                             "check passed. See the checks below." + (f" {result['error']}" if result["error"] else ""))
    else:
        result["outcome"] = "not_reproduced"
        detail = ("MySQL reported a lock wait timeout (error 1205) instead of detecting a deadlock. "
                  "This happens when innodb_deadlock_detect is OFF."
                  if timeout_seen else "MySQL did not raise a deadlock error in this run.")
        result["summary"] = "No deadlock was verified. " + detail + (f" {result['error']}" if result["error"] else "")


# ============================================================
# Command line
# ============================================================

def format_report(result):
    lines = [f"Real MySQL deadlock test   database={result['database']}  table={result['table']}",
             f"Outcome: {result['outcome'].upper()}", result["summary"], ""]
    server = result.get("server") or {}
    if server.get("version"):
        lines.append(f"Server: MySQL {server['version']}  innodb_deadlock_detect={server.get('deadlock_detect')}  "
                     f"isolation={server.get('isolation')}")
    lines.append("")
    lines.append("Timeline (real):")
    for e in result["events"]:
        lines.append(f"  {e['t']:7.3f}s  {e['actor']:<28} {e['message']}")
    lines.append("")
    lines.append("Checks:")
    for c in result["checks"]:
        lines.append(f"  [{c['status'].upper():>4}] {c['label']}")
    if result.get("innodb_report"):
        lines += ["", "MySQL's own deadlock report (excerpt):", result["innodb_report"][:1500]]
    elif result.get("innodb_report_note"):
        lines += ["", result["innodb_report_note"]]
    c = result["cleanup"]
    lines += ["", f"Cleanup: deleted {c['deleted']} row(s), remaining {c['remaining']}, "
                  f"ok={c['ok']}, table kept (empty): {c['table_kept']}"]
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    return "\n".join(lines)


def main(argv=None, connect=None, out=print):
    argv = list(sys.argv[1:] if argv is None else argv)
    from config import get_config            # same MYSQL_* settings as the app; no DB access, no worker

    cfg = get_config().DB_CONFIG
    target = (f"database '{cfg['database']}' on {cfg['host']}:{cfg['port']} as '{cfg['user']}', "
              f"table '{TEST_TABLE}'")
    if "--yes" not in argv:
        out(f"This will create (if missing) the dedicated test table and run two real, deadlocking "
            f"transactions in {target}. It touches no other table and drops nothing.")
        out("Re-run with --yes to proceed.")
        return 2
    out(f"Running against {target} ...")
    result = run_real_deadlock_test(cfg, connect=connect)
    out(format_report(result))
    return 0 if result["outcome"] == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
