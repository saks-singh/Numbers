"""Does the Python agree with the DDL, and does the DDL agree with the skill?

The point of this file is that it needs no Postgres. The schema is three
codebases meeting at a column name -- `bootbench.py` produces the metric,
`src/db/schema.py` spells it for the INSERT, and `schema.sql` declares where
it lands -- and the usual way that breaks is a metric added to one of the
three. A live-server test would catch it only after a deploy; this catches
it on a laptop.

The most valuable assertion here is `test_seconds_keys_match_bootbench`. The
skill and this database are separate codebases joined only by a JSON
document, so a new metric appearing in the status doc with nowhere to land
would otherwise go unnoticed for as long as nobody looked at a chart.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE / "fixtures"))

import make_golden  # noqa: E402  (also puts the skill's scripts/ on sys.path)

from src.db import queries, schema  # noqa: E402

EXPECTED_TABLES = {
    "device", "run", "boot", "scheduler_state", "scheduler_decision",
    "schema_migration",
}
EXPECTED_VIEWS = {"run_latest", "boot_trend"}
EXPECTED_ENUMS = {"run_status", "trigger_source", "decision_action"}
EXPECTED_INDEXES = {
    "run_device_build_idx": "run",
    "run_device_started_idx": "run",
    "run_active_idx": "run",
    "boot_run_idx": "boot",
    "boot_overheads_gin_idx": "boot",
    "decision_device_time_idx": "scheduler_decision",
}

DDL = schema.SCHEMA_SQL.read_text(encoding="utf-8")


class ParserSanity(unittest.TestCase):
    """If the parser stops understanding the DDL, every other test in this
    file would silently check an empty dict. These assertions are what make
    the rest meaningful."""

    def test_every_expected_table_parsed(self):
        self.assertEqual(set(schema.parse_tables()), EXPECTED_TABLES)

    def test_every_expected_view_parsed(self):
        self.assertEqual(set(schema.parse_views()), EXPECTED_VIEWS)

    def test_every_expected_enum_parsed(self):
        self.assertEqual(set(schema.parse_enums()), EXPECTED_ENUMS)

    def test_every_expected_index_parsed(self):
        self.assertEqual(schema.parse_indexes(), EXPECTED_INDEXES)

    def test_table_constraints_are_not_mistaken_for_columns(self):
        # `boot` ends with UNIQUE (run_id, log_name); a naive parser reports
        # 'UNIQUE' as a 17th column and then every column test passes
        # vacuously.
        for name, columns in schema.parse_tables().items():
            for column in columns:
                self.assertRegex(column, r"^[a-z][a-z0-9_]*$",
                                 f"{name}.{column} is not a column name")

    def test_numeric_precision_comma_does_not_split_a_column(self):
        # NUMERIC(10,3) contains a comma that does not separate columns.
        self.assertIn("nhlos_s", schema.parse_tables()["boot"])
        self.assertNotIn("3)", schema.parse_tables()["boot"])


class ColumnLists(unittest.TestCase):

    def setUp(self):
        self.tables = schema.parse_tables()

    def test_boot_columns_match_the_ddl_exactly(self):
        # Bidirectional: a column in the DDL that BOOT_COLUMNS omits is a
        # column ingestion will never write, which is just as broken as the
        # reverse.
        ddl = [c for c in self.tables["boot"] if c != "boot_id"]
        self.assertEqual(sorted(schema.BOOT_COLUMNS), sorted(ddl))
        self.assertEqual(len(schema.BOOT_COLUMNS), len(set(schema.BOOT_COLUMNS)))

    def test_device_columns_exist_and_omit_the_generated_ones(self):
        ddl = set(self.tables["device"])
        self.assertTrue(set(schema.DEVICE_COLUMNS) <= ddl,
                        sorted(set(schema.DEVICE_COLUMNS) - ddl))
        # first_seen_at must never be in the upsert list: re-reading the
        # config would reset "benchmarked since March" to today.
        for generated in ("first_seen_at", "last_seen_at"):
            self.assertIn(generated, ddl)
            self.assertNotIn(generated, schema.DEVICE_COLUMNS)

    def test_run_ingest_columns_exist(self):
        ddl = set(self.tables["run"])
        self.assertTrue(set(schema.RUN_INGEST_COLUMNS) <= ddl,
                        sorted(set(schema.RUN_INGEST_COLUMNS) - ddl))

    def test_run_ingest_columns_exclude_the_queue_side(self):
        # Ingest runs after the agent finishes and must not reset the
        # columns the enqueue decided. `status` especially: the worker
        # derives the terminal status itself from exit_code and boot counts.
        for queue_side in ("status", "queued_at", "trigger_source",
                           "triggered_by", "parent_run_id", "cancel_requested",
                           "acknowledged_at", "device_id"):
            self.assertNotIn(queue_side, schema.RUN_INGEST_COLUMNS)

    def test_every_duration_column_carries_the_seconds_suffix(self):
        for column in schema.SECONDS_COLUMNS:
            self.assertTrue(column.endswith("_s"), column)

    def test_every_numeric_column_in_boot_is_a_known_metric(self):
        # The reverse of the chart test: a NUMERIC column nothing in
        # SECONDS_COLUMNS names would never be selected, stored as NULL
        # forever, and look like a working metric in `\d boot`.
        block = re.search(r"CREATE TABLE IF NOT EXISTS boot \((.*?)\n\);",
                          DDL, re.S).group(1)
        numeric = {line.split()[0] for line in block.splitlines()
                   if "NUMERIC" in line and line.strip()
                   and not line.strip().startswith("--")}
        self.assertEqual(numeric, set(schema.SECONDS_COLUMNS))


class BootbenchCrossCheck(unittest.TestCase):
    """The schema against the skill that fills it."""

    def test_seconds_keys_match_bootbench(self):
        # build_run() is the real thing, run over the real fixtures -- not a
        # hand-written list that could drift alongside the one it checks.
        boots = make_golden.build_boots()
        self.assertTrue(boots, "fixture harness produced no boots")
        for boot in boots:
            with self.subTest(log_name=boot["log_name"]):
                self.assertEqual(set(boot["seconds"]),
                                 set(schema.SECONDS_KEYS))

    def test_every_metric_key_has_a_column(self):
        columns = set(schema.parse_tables()["boot"])
        for key in schema.SECONDS_KEYS:
            with self.subTest(key=key):
                self.assertIn(schema.seconds_column(key), columns)

    def test_metric_rows_are_all_in_metric_keys(self):
        # METRIC_ROWS is what the skill's report table shows. Those six are
        # the ones a dashboard column header refers to, so they must be
        # exactly the leading six here and in display order.
        import bootbench as bb

        displayed = [row[0] for row in bb.METRIC_ROWS]
        self.assertEqual(displayed, list(schema.METRIC_KEYS))


class ChartQueries(unittest.TestCase):
    """The ::float8 selection is built from SECONDS_COLUMNS, so it can only
    break by referring to a column the source relation lacks."""

    def test_float_selection_names_only_real_boot_columns(self):
        columns = set(schema.parse_tables()["boot"])
        for fragment in queries._float_selection().split(", "):
            name = fragment.split("::")[0]
            with self.subTest(column=name):
                self.assertIn(name, columns)

    def test_float_selection_casts_every_duration_column(self):
        selection = queries._float_selection()
        for column in schema.SECONDS_COLUMNS:
            self.assertIn(f"{column}::float8 AS {column}", selection)

    def test_boot_trend_carries_every_duration_column(self):
        # The regression this catches for real: the view was written with a
        # hand-picked seven of the sixteen, so trend() -- which selects all
        # sixteen -- raised UndefinedColumn.
        view = set(schema.parse_view_columns()["boot_trend"])
        missing = sorted(set(schema.SECONDS_COLUMNS) - view)
        self.assertEqual(missing, [], f"boot_trend lacks {missing}")

    def test_boot_trend_carries_the_columns_trend_filters_on(self):
        view = set(schema.parse_view_columns()["boot_trend"])
        # Pinning phase and boot_index is what keeps the series like-for-like
        # (rmtfs is masked from boot 2 onward); the other three are the
        # honesty filters the plan calls for.
        for column in ("device_id", "phase", "boot_index", "cmdline_has_debug",
                       "reflashed", "build_number", "started_at", "status",
                       "run_id", "build_folder", "log_name",
                       "overall_overheads"):
            with self.subTest(column=column):
                self.assertIn(column, view)


class EnumVocabulary(unittest.TestCase):
    """Status strings are spelled in Python and declared in the DDL. A typo
    is a write-time error on a path that only runs at 01:00."""

    def _enum_values(self, name):
        block = re.search(
            rf"CREATE TYPE {name} AS ENUM \((.*?)\);", DDL, re.S).group(1)
        return set(re.findall(r"'([a-z_]+)'", block))

    def test_active_statuses_are_real_run_statuses(self):
        values = self._enum_values("run_status")
        self.assertTrue(set(queries.ACTIVE_STATUSES) <= values)

    def test_benchmarked_statuses_are_real_run_statuses(self):
        values = self._enum_values("run_status")
        self.assertTrue(set(queries.BENCHMARKED_STATUSES) <= values)

    def test_partial_counts_as_benchmarked(self):
        # Deliberate: the build was measured, just not completely. Dropping
        # it would re-run the same build every tick forever.
        self.assertIn("partial", queries.BENCHMARKED_STATUSES)

    def test_every_status_the_queries_filter_on_is_declared(self):
        values = self._enum_values("run_status")
        for literal in re.findall(r"status IN \(([^)]*)\)", DDL):
            for value in re.findall(r"'([a-z_]+)'", literal):
                self.assertIn(value, values)


class SchemaAndMigrationAgree(unittest.TestCase):
    """schema.sql is for reading and `psql -f` on a fresh box; migrations/ is
    what executes. The header of each says to keep them in step, which is
    exactly the kind of instruction that decays without a test."""

    def setUp(self):
        self.migration = (schema.MIGRATIONS_DIR / "001_init.sql").read_text(
            encoding="utf-8")

    def test_same_tables_and_columns(self):
        self.assertEqual(schema.parse_tables(),
                         schema.parse_tables(self.migration))

    def test_same_views(self):
        self.assertEqual(schema.parse_views(),
                         schema.parse_views(self.migration))
        self.assertEqual(schema.parse_view_columns(),
                         schema.parse_view_columns(self.migration))

    def test_same_indexes_and_enums(self):
        self.assertEqual(schema.parse_indexes(),
                         schema.parse_indexes(self.migration))
        self.assertEqual(schema.parse_enums(),
                         schema.parse_enums(self.migration))


class Idempotency(unittest.TestCase):
    """`initdb` is expected to be run by someone unsure whether they already
    ran it, so the DDL has to be a no-op the second time independently of
    the bookkeeping table -- which is itself created by the migration that
    would have to record it."""

    def test_every_create_table_is_if_not_exists(self):
        for name in re.findall(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)",
                               DDL):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {name}", DDL)

    def test_every_create_index_is_if_not_exists(self):
        for name in re.findall(r"CREATE INDEX\s+(?:IF NOT EXISTS\s+)?(\w+)",
                               DDL):
            self.assertIn(f"CREATE INDEX IF NOT EXISTS {name}", DDL)

    def test_every_view_is_or_replace(self):
        for name in schema.parse_views():
            self.assertIn(f"CREATE OR REPLACE VIEW {name}", DDL)

    def test_every_enum_swallows_duplicate_object(self):
        # CREATE TYPE has no IF NOT EXISTS, so each one needs the DO block.
        self.assertEqual(DDL.count("EXCEPTION WHEN duplicate_object THEN NULL"),
                         len(EXPECTED_ENUMS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
