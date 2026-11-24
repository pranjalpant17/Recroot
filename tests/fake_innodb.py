"""
A small in-memory MODEL of the InnoDB behaviour the real deadlock test relies
on. THIS IS NOT MYSQL and passing tests against it does NOT prove the feature
works on a real server; it verifies the orchestration (threads, barrier,
retry, cleanup, safety checks) deterministically and without a database.

What it models
  * row locks held until COMMIT/ROLLBACK; a conflicting UPDATE waits
  * a wait-for graph; on a cycle the server picks a victim, rolls that
    transaction back and raises error 1213 to it (the victim is configurable:
    "requester" = the one that closed the cycle, "other" = the one already
    waiting; real MySQL decides this itself, so the code must handle both)
  * lock wait timeout: error 1205 to the waiting statement only; the
    transaction stays open and keeps its locks (like InnoDB's default)
  * innodb_deadlock_detect OFF: no detection, so waits end in 1205
  * undo on rollback, and implicit rollback when a connection closes
  * SHOW ENGINE INNODB STATUS with a LATEST DETECTED DEADLOCK section
  * hooks to inject connection failures, hangs and PROCESS-privilege errors

What it refuses
  Any SQL statement it does not recognise raises AssertionError, so every
  statement the feature issues must be one of the known, reviewed shapes, and
  every recorded statement can be audited by the tests.
"""

import itertools
import re
import threading
import time
from datetime import datetime

from tests.test_deadlock_handling import OperationalError


def _oe(code, message):
    return OperationalError(code, message)


DEADLOCK = (1213, "Deadlock found when trying to get lock; try restarting transaction")
TIMEOUT = (1205, "Lock wait timeout exceeded; try restarting transaction")


class _Txn:
    _ids = itertools.count(1000)

    def __init__(self, conn):
        self.id = next(_Txn._ids)
        self.conn = conn
        self.undo = []          # (table, row_id, old_counter)
        self.locks = set()      # (table, row_id)
        self.aborted = False
        self.last_sql = ""


class FakeMySQLServer:
    def __init__(self, deadlock_detect=True, victim="requester", version="8.0.fake",
                 process_privilege=True, metrics_enabled=True):
        self.cv = threading.Condition()
        self.deadlock_detect = deadlock_detect
        self.victim = victim
        self.version = version
        self.process_privilege = process_privilege
        self.metrics_enabled = metrics_enabled

        self.tables = {}            # name -> {"comment", "engine", "rows", "next_id"}
        self.locks = {}             # (table, id) -> _Txn
        self.waiting = {}           # _Txn -> (table, id)
        self.statements = []        # (connection_id, interpolated sql)
        self.connections = []
        self.deadlock_count = 0
        self.last_deadlock = None

        # failure-injection hooks
        self.fail_connect_numbers = set()   # 1-based connect() call numbers that fail
        self.fail_next_updates = []         # exceptions raised by the next UPDATEs
        self.hang_updates = False
        self.hang_event = threading.Event()
        self.hang_seconds = 5.0
        self._connect_calls = 0

    # ---------- connection factory (drop-in for pymysql.connect) ----------

    def connect(self, **kwargs):
        with self.cv:
            self._connect_calls += 1
            number = self._connect_calls
        if number in self.fail_connect_numbers:
            raise _oe(2003, "Can't connect to MySQL server")
        lock_wait = 50
        match = re.search(r"innodb_lock_wait_timeout\s*=\s*(\d+)", kwargs.get("init_command") or "")
        if match:
            lock_wait = int(match.group(1))
        conn = FakeConnection(self, number, bool(kwargs.get("autocommit")), lock_wait)
        self.connections.append(conn)
        return conn

    # ---------- helpers ----------

    @staticmethod
    def interpolate(sql, params):
        if not params:
            return sql
        shown = tuple(repr(p) if isinstance(p, str) else str(p) for p in params)
        return sql % shown

    def _release(self, txn):
        for key in list(txn.locks):
            if self.locks.get(key) is txn:
                del self.locks[key]
        txn.locks.clear()

    def _undo(self, txn):
        for table, row_id, old in reversed(txn.undo):
            row = self.tables[table]["rows"].get(row_id)
            if row is not None:
                row["counter"] = old
        txn.undo.clear()

    def finish(self, txn, commit):
        with self.cv:
            if txn is None:
                return
            if not commit and not txn.aborted:
                self._undo(txn)
            txn.undo.clear()
            self._release(txn)
            self.waiting.pop(txn, None)
            self.cv.notify_all()

    def _abort(self, victim, other):
        self._undo(victim)
        self._release(victim)
        victim.aborted = True
        self.waiting.pop(victim, None)
        self.deadlock_count += 1
        self.last_deadlock = (
            "------------------------\n"
            "LATEST DETECTED DEADLOCK\n"
            "------------------------\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 0x7f00fake\n"
            f"*** (1) TRANSACTION:\nTRANSACTION {other.id}, ACTIVE 1 sec starting index read\n"
            f"{other.last_sql}\n"
            f"*** (2) TRANSACTION:\nTRANSACTION {victim.id}, ACTIVE 1 sec starting index read\n"
            f"{victim.last_sql}\n"
            f"*** WE ROLL BACK TRANSACTION ({victim.id})\n")
        self.cv.notify_all()

    def _cycle_from(self, txn):
        path, current = [txn], txn
        while True:
            key = self.waiting.get(current)
            if key is None:
                return None
            owner = self.locks.get(key)
            if owner is None:
                return None
            if owner is txn:
                return path
            if owner in path:
                return None
            path.append(owner)
            current = owner

    def status_text(self):
        body = ("=====================================\nINNODB MONITOR OUTPUT (fake)\n"
                "=====================================\n"
                "------------\nSEMAPHORES\n------------\n(none)\n")
        if self.last_deadlock:
            body += self.last_deadlock
        body += "------------\nTRANSACTIONS\n------------\nTrx id counter 1\n"
        return body

    # ---------- statement execution ----------

    def execute(self, conn, sql, params):
        sql_n = " ".join(sql.split())
        text = self.interpolate(sql_n, params)
        with self.cv:
            self.statements.append((conn.id, text))

        m = re.fullmatch(r"CREATE TABLE IF NOT EXISTS (\w+) \(.*\) ENGINE=(\w+) .*COMMENT='(.*)'", sql_n)
        if m:
            with self.cv:
                self.tables.setdefault(m.group(1), {"comment": m.group(3), "engine": m.group(2),
                                                    "rows": {}, "next_id": 1})
            return 0, [], None

        if sql_n.startswith("SELECT TABLE_COMMENT AS table_comment") and "information_schema.TABLES" in sql_n:
            with self.cv:
                table = self.tables.get(params[0])
                rows = [] if table is None else [{"table_comment": table["comment"],
                                                  "table_engine": table["engine"]}]
            return 0, rows, None

        if sql_n == "SELECT VERSION() AS v":
            return 0, [{"v": self.version}], None
        if sql_n == "SELECT @@innodb_deadlock_detect AS v":
            return 0, [{"v": "1" if self.deadlock_detect else "0"}], None
        if sql_n == "SELECT @@transaction_isolation AS v":
            return 0, [{"v": "REPEATABLE-READ"}], None
        if sql_n.startswith("SELECT `COUNT` AS metric_count FROM information_schema.INNODB_METRICS"):
            if not self.metrics_enabled:
                raise _oe(1227, "Access denied; you need the PROCESS privilege")
            return 0, [{"metric_count": self.deadlock_count}], None
        if sql_n == "SHOW ENGINE INNODB STATUS":
            if not self.process_privilege:
                raise _oe(1227, "Access denied; you need (at least one of) the PROCESS privilege(s)")
            return 0, [{"Type": "InnoDB", "Name": "", "Status": self.status_text()}], None

        if re.fullmatch(r"SET SESSION innodb_lock_wait_timeout = \d+", sql_n):
            return 0, [], None

        m = re.fullmatch(r"DELETE FROM (\w+) WHERE created_at < NOW\(\) - INTERVAL 1 DAY", sql_n)
        if m:
            self._need_table(m.group(1))
            return 0, [], None

        m = re.fullmatch(r"DELETE FROM (\w+) WHERE run_id = %s", sql_n)
        if m:
            table = self._need_table(m.group(1))
            with self.cv:
                doomed = [i for i, r in table["rows"].items() if r["run_id"] == params[0]]
                for i in doomed:
                    del table["rows"][i]
            return len(doomed), [], None

        m = re.fullmatch(r"INSERT INTO (\w+) \(run_id, label, counter\) VALUES \(%s, %s, 0\)", sql_n)
        if m:
            table = self._need_table(m.group(1))
            with self.cv:
                row_id = table["next_id"]
                table["next_id"] += 1
                table["rows"][row_id] = {"id": row_id, "run_id": params[0], "label": params[1],
                                         "counter": 0}
            return 1, [], row_id

        m = re.fullmatch(r"SELECT id, label, counter FROM (\w+) WHERE run_id = %s ORDER BY id", sql_n)
        if m:
            table = self._need_table(m.group(1))
            with self.cv:
                rows = [{"id": r["id"], "label": r["label"], "counter": r["counter"]}
                        for _, r in sorted(table["rows"].items()) if r["run_id"] == params[0]]
            return 0, rows, None

        m = re.fullmatch(r"SELECT COUNT\(\*\) AS n FROM (\w+) WHERE run_id = %s", sql_n)
        if m:
            table = self._need_table(m.group(1))
            with self.cv:
                n = sum(1 for r in table["rows"].values() if r["run_id"] == params[0])
            return 0, [{"n": n}], None

        m = re.fullmatch(r"UPDATE (\w+) SET counter = counter \+ 1 WHERE id = %s", sql_n)
        if m:
            return self._update(conn, m.group(1), params[0], text)

        raise AssertionError(f"Unrecognised SQL (not on the reviewed list): {sql_n}")

    def _need_table(self, name):
        with self.cv:
            if name not in self.tables:
                raise _oe(1146, f"Table '{name}' doesn't exist")
            return self.tables[name]

    def _update(self, conn, table_name, row_id, text):
        table = self._need_table(table_name)
        if self.fail_next_updates:
            raise self.fail_next_updates.pop(0)
        if self.hang_updates:
            self.hang_event.wait(self.hang_seconds)
            raise _oe(2013, "Lost connection to MySQL server during query")

        txn = conn.ensure_txn()
        key = (table_name, row_id)
        with self.cv:
            txn.last_sql = text
            deadline = time.monotonic() + conn.lock_wait
            while True:
                if txn.aborted:
                    self.waiting.pop(txn, None)
                    raise _oe(*DEADLOCK)
                owner = self.locks.get(key)
                if owner is None or owner is txn:
                    break
                self.waiting[txn] = key
                if self.deadlock_detect:
                    cycle = self._cycle_from(txn)
                    if cycle:
                        victim = txn if self.victim == "requester" else cycle[1]
                        other = cycle[1] if victim is txn else txn
                        self._abort(victim, other)
                        if victim is txn:
                            raise _oe(*DEADLOCK)
                        continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.waiting.pop(txn, None)
                    raise _oe(*TIMEOUT)          # statement fails; txn stays open (InnoDB default)
                self.cv.wait(min(remaining, 0.05))
            self.waiting.pop(txn, None)
            self.locks[key] = txn
            txn.locks.add(key)
            row = table["rows"][row_id]
            txn.undo.append((table_name, row_id, row["counter"]))
            row["counter"] += 1
        return 1, [], None


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self.lastrowid = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.check_open()
        implicit = self.conn.autocommit and self.conn.txn is None
        try:
            count, rows, last = self.conn.server.execute(self.conn, sql, params)
        except OperationalError as exc:
            if exc.args and exc.args[0] == 1213:
                self.conn.txn = None             # InnoDB already rolled the whole txn back
            if implicit:
                self.conn.rollback()
            raise
        if implicit:
            self.conn.commit()
        self._rows, self.lastrowid, self.rowcount = rows, last, count
        return count

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, server, conn_id, autocommit, lock_wait):
        self.server = server
        self.id = conn_id
        self.autocommit = autocommit
        self.lock_wait = lock_wait
        self.txn = None
        self.closed = False
        self.rollback_calls = 0

    def check_open(self):
        if self.closed:
            raise _oe(2006, "MySQL server has gone away")

    def ensure_txn(self):
        if self.txn is None:
            self.txn = _Txn(self)
        return self.txn

    def begin(self):
        self.check_open()
        self.txn = _Txn(self)

    def cursor(self):
        self.check_open()
        return FakeCursor(self)

    def commit(self):
        self.check_open()
        txn, self.txn = self.txn, None
        self.server.finish(txn, commit=True)

    def rollback(self):
        self.check_open()
        self.rollback_calls += 1               # explicit ROLLBACK statements issued by the client
        txn, self.txn = self.txn, None
        self.server.finish(txn, commit=False)

    def close(self):
        if self.closed:
            return
        txn, self.txn = self.txn, None
        self.server.finish(txn, commit=False)     # server rolls back on disconnect
        self.closed = True
