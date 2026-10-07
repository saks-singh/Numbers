"""Migration bookkeeping, with a stub connection instead of a server.

Everything interesting about `apply_migrations` is decision-making -- what
to skip, what to re-run, what to refuse, and what to warn about -- and none
of it needs Postgres. The one thing a stub cannot check is whether the DDL
is valid SQL; that is `initdb` on the Linux box, and `test_schema_consistency`
covers the shape of it in the meantime.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.db import migrate, schema  # noqa: E402


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if self.conn.raise_on and self.conn.raise_on in sql:
            raise RuntimeError("syntax error at or near something")
        if "FROM schema_migration" in sql:
            if not self.conn.has_table:
                raise RuntimeError('relation "schema_migration" does not exist')
            self._rows = list(self.conn.recorded.items())
        elif "INSERT INTO schema_migration" in sql:
            self.conn.recorded[params[0]] = params[1]
            self._rows = []
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Just enough psycopg to drive migrate.py."""

    def __init__(self, recorded=None, has_table=True, raise_on=None):
        self.recorded = dict(recorded or {})
        self.has_table = has_table
        self.raise_on = raise_on
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, **_kwargs):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def ddl_applied(self):
        return [sql for sql, _p in self.executed
                if "schema_migration" not in sql]


class MigrationNames(unittest.TestCase):
    """Lexical order is the apply order, so the name is load-bearing."""

    def test_accepts_a_zero_padded_name(self):
        self.assertEqual(
            migrate.validate_names([Path("001_init.sql")]), [])

    def test_rejects_names_that_would_apply_out_of_order(self):
        for name in ("9_init.sql", "init.sql", "01_init.sql",
                     "0001_init.sql", "001-init.sql", "001_Init.sql",
                     "001_init.SQL", "001_init.psql"):
            with self.subTest(name=name):
                self.assertEqual(
                    len(migrate.validate_names([Path(name)])), 1, name)

    def test_reports_every_problem_at_once(self):
        # initdb prints them all rather than making the operator fix one,
        # re-run, and discover the next.
        problems = migrate.validate_names(
            [Path("ok_001.sql"), Path("9_x.sql"), Path("001_good.sql")])
        self.assertEqual(len(problems), 2)

    def test_unpadded_prefix_really_does_sort_wrong(self):
        # The reason the rule exists, asserted rather than asserted-about.
        self.assertLess("10_x.sql", "9_x.sql")
        self.assertLess("009_x.sql", "010_x.sql")


class WithTempMigrations(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patcher = patch.object(schema, "MIGRATIONS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, body):
        (self.dir / name).write_text(body, encoding="utf-8")
        return self.dir / name


class ApplyMigrations(WithTempMigrations):

    def test_fresh_database_applies_in_name_order(self):
        self.write("002_second.sql", "SELECT 2;")
        self.write("001_first.sql", "SELECT 1;")
        conn = FakeConn(has_table=False)

        actions = migrate.apply_migrations(conn)

        self.assertEqual([(n, a) for n, a, _d in actions],
                         [("001_first.sql", "applied"),
                          ("002_second.sql", "applied")])
        self.assertEqual(conn.ddl_applied(), ["SELECT 1;", "SELECT 2;"])

    def test_each_migration_commits_separately(self):
        # A failure leaves the migrations before it applied and recorded,
        # which is what you want when the failure is in the one you just
        # wrote.
        self.write("001_a.sql", "SELECT 1;")
        self.write("002_b.sql", "SELECT 2;")
        conn = FakeConn(has_table=False)
        migrate.apply_migrations(conn)
        self.assertEqual(conn.commits, 2)

    def test_second_run_skips_everything(self):
        path = self.write("001_first.sql", "SELECT 1;")
        conn = FakeConn(has_table=False)
        migrate.apply_migrations(conn)

        again = FakeConn(recorded=conn.recorded)
        actions = migrate.apply_migrations(again)

        self.assertEqual([a for _n, a, _d in actions], ["skipped"])
        self.assertEqual(again.ddl_applied(), [],
                         "re-running must not execute the DDL again")
        self.assertEqual(
            conn.recorded[path.name], migrate._sha256("SELECT 1;"))

    def test_an_edited_applied_migration_is_reported_as_changed(self):
        # The mistake this catches: the edit silently does not reach any
        # database that already ran the file, and the divergence stays
        # invisible until a query fails in production.
        self.write("001_first.sql", "SELECT 1;")
        conn = FakeConn(has_table=False)
        migrate.apply_migrations(conn)
        self.write("001_first.sql", "SELECT 1; SELECT 2;")

        actions = migrate.apply_migrations(FakeConn(recorded=conn.recorded))
        name, action, detail = actions[0]
        self.assertEqual(action, "changed")
        self.assertIn("did not run the current contents", detail)

    def test_changed_warns_rather_than_refuses(self):
        # During development, before the file has reached a real server,
        # editing it is perfectly reasonable.
        self.write("001_first.sql", "SELECT 1;")
        conn = FakeConn(has_table=False)
        migrate.apply_migrations(conn)
        self.write("001_first.sql", "SELECT 9;")
        try:
            migrate.apply_migrations(FakeConn(recorded=conn.recorded))
        except migrate.MigrationError as e:
            self.fail(f"should warn, not raise: {e}")

    def test_a_null_recorded_sha_is_not_treated_as_divergence(self):
        # The sha column is nullable, so a row written before hashing
        # existed must not be reported as changed forever -- nor, which was
        # the actual bug, be re-applied because `.get()` returned None for
        # both "absent" and "present but NULL".
        self.write("001_first.sql", "SELECT 1;")
        conn = FakeConn(recorded={"001_first.sql": None})
        actions = migrate.apply_migrations(conn)
        self.assertEqual([a for _n, a, _d in actions], ["skipped"])
        self.assertEqual(conn.ddl_applied(), [])

    def test_dry_run_changes_nothing(self):
        self.write("001_first.sql", "SELECT 1;")
        conn = FakeConn(has_table=False)
        actions = migrate.apply_migrations(conn, dry_run=True)
        self.assertEqual([a for _n, a, _d in actions], ["pending"])
        self.assertEqual(conn.ddl_applied(), [])
        self.assertEqual(conn.recorded, {})
        self.assertEqual(conn.commits, 0)

    def test_a_bad_name_refuses_before_applying_anything(self):
        self.write("001_good.sql", "SELECT 1;")
        self.write("nope.sql", "SELECT 2;")
        conn = FakeConn(has_table=False)
        with self.assertRaises(migrate.MigrationError):
            migrate.apply_migrations(conn)
        self.assertEqual(conn.ddl_applied(), [],
                         "the valid migration must not run either")

    def test_an_empty_migrations_dir_is_an_error_not_a_silent_success(self):
        # An initdb that prints nothing and exits 0 against a database with
        # no tables is the worst possible outcome.
        with self.assertRaises(migrate.MigrationError) as ctx:
            migrate.apply_migrations(FakeConn(has_table=False))
        self.assertIn("no migrations found", str(ctx.exception))

    def test_a_failing_migration_rolls_back_and_names_the_file(self):
        self.write("001_ok.sql", "SELECT 1;")
        self.write("002_bad.sql", "SELECT bad_syntax;")
        # has_table=True so the applied() probe succeeds and the only
        # rollback counted is the one the failure causes.
        conn = FakeConn(has_table=True, raise_on="bad_syntax")
        with self.assertRaises(migrate.MigrationError) as ctx:
            migrate.apply_migrations(conn)
        self.assertIn("002_bad.sql", str(ctx.exception))
        self.assertEqual(conn.rollbacks, 1)
        self.assertIn("001_ok.sql", conn.recorded)
        self.assertNotIn("002_bad.sql", conn.recorded)


class AppliedProbe(WithTempMigrations):

    def test_missing_bookkeeping_table_is_not_an_error(self):
        self.assertEqual(migrate.applied(FakeConn(has_table=False)), {})

    def test_missing_bookkeeping_table_rolls_back_the_connection(self):
        # In Postgres a failed statement aborts the whole transaction, so
        # without this every subsequent statement on this connection fails
        # with "current transaction is aborted" -- far from the cause.
        conn = FakeConn(has_table=False)
        migrate.applied(conn)
        self.assertEqual(conn.rollbacks, 1)

    def test_reads_what_is_recorded(self):
        conn = FakeConn(recorded={"001_x.sql": "abc"})
        self.assertEqual(migrate.applied(conn), {"001_x.sql": "abc"})


class Status(WithTempMigrations):

    def test_reports_pending_changed_and_orphaned(self):
        self.write("001_applied.sql", "SELECT 1;")
        self.write("002_edited.sql", "SELECT 2;")
        self.write("003_new.sql", "SELECT 3;")
        conn = FakeConn(recorded={
            "001_applied.sql": migrate._sha256("SELECT 1;"),
            "002_edited.sql": migrate._sha256("SELECT 2; -- old"),
            "000_deleted.sql": "whatever",
        })

        state = migrate.status(conn)

        self.assertEqual(state["pending"], ["003_new.sql"])
        self.assertEqual(state["changed"], ["002_edited.sql"])
        self.assertEqual(state["orphaned"], ["000_deleted.sql"])
        self.assertIn("001_applied.sql", state["applied"])

    def test_a_null_sha_counts_as_applied_not_pending(self):
        self.write("001_first.sql", "SELECT 1;")
        state = migrate.status(FakeConn(recorded={"001_first.sql": None}))
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["changed"], [])


class RealMigrations(unittest.TestCase):
    """The shipped files, not a fixture."""

    def test_the_shipped_names_are_valid(self):
        self.assertEqual(migrate.validate_names(), [])

    def test_there_is_at_least_one(self):
        self.assertTrue(schema.migration_files())

    def test_001_init_is_first(self):
        self.assertEqual(schema.migration_files()[0].name, "001_init.sql")

    def test_the_shipped_ddl_applies_cleanly_against_a_stub(self):
        # Not a SQL check -- a bookkeeping check: every shipped file is read,
        # hashed, executed once, and recorded.
        conn = FakeConn(has_table=False)
        actions = migrate.apply_migrations(conn)
        self.assertTrue(all(a == "applied" for _n, a, _d in actions))
        self.assertEqual(len(conn.recorded), len(schema.migration_files()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
