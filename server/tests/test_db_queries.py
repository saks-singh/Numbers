"""The SQL the coordinator builds, checked without a server.

A query is mostly string construction plus a parameter list, and both are
inspectable. What a stub cannot tell you is whether Postgres accepts the
result -- that is `test_db_live.py`, which skips unless BOOTBENCH_TEST_DSN
is set. So these tests deliberately assert the properties that *would*
survive a rename: that every duration column is cast, that the enqueue and
its notify share a transaction, that the device upsert cannot clobber
first_seen_at.
"""

from __future__ import annotations

import datetime
import decimal
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.db import queries, schema  # noqa: E402
from src.inventory import Device  # noqa: E402


class RecordingCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        rows = self.conn.rows
        return rows[0] if rows else None

    def fetchall(self):
        return list(self.conn.rows)


class RecordingConn:
    """Captures SQL instead of running it. `row_factory` is accepted and
    ignored so the dict_row queries are reachable without psycopg."""

    def __init__(self, rows=()):
        self.executed = []
        self.rows = list(rows)
        self.commits = 0

    def cursor(self, **_kwargs):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    @property
    def sql(self):
        return [s for s, _p in self.executed]

    def only(self):
        self.assertions = len(self.executed)
        return self.executed[0]


DEVICE = Device(
    device_id="iq-9075-evk-01",
    target="iq-9075-evk",
    agent_url="http://bench-win-01:8765",
    com_port="COM7",
    tac_port="VTP8",
    adb_serial="1a2b3c4d5e",
    num_boots=3,
    notes="lab rack 3",
)

_patches = []


def setUpModule():
    # `row_factory=dict_row` is a psycopg import, and psycopg is a
    # coordinator-only dependency that is deliberately absent from the
    # Windows side and from any checkout that has not installed
    # requirements.txt. The row factory has no bearing on the SQL being
    # asserted here, so it is stubbed rather than required.
    _patches.append(patch.object(queries, "_dict_row", lambda: None))
    for patcher in _patches:
        patcher.start()


def tearDownModule():
    for patcher in _patches:
        patcher.stop()
    _patches.clear()


class FloatCasting(unittest.TestCase):
    """Risk 9: NUMERIC comes back as Decimal and json.dumps refuses it. The
    cast belongs in SQL, at the edge, because a missing cast fails
    immediately and visibly whereas a Python conversion is easy to forget in
    one branch."""

    def test_selection_is_built_from_the_canonical_list(self):
        selection = queries._float_selection()
        self.assertEqual(selection.count("::float8"),
                         len(schema.SECONDS_COLUMNS))

    def test_selection_aliases_back_to_the_column_name(self):
        # Without `AS <col>` the result key would be 'float8' for all 16.
        for column in schema.SECONDS_COLUMNS:
            self.assertIn(f"{column}::float8 AS {column}",
                          queries._float_selection())

    def test_prefix_qualifies_every_column(self):
        selection = queries._float_selection("b.")
        self.assertEqual(selection.count("b."), len(schema.SECONDS_COLUMNS))
        self.assertNotIn("AS b.", selection,
                         "the alias must not carry the table prefix")

    def test_every_query_feeding_a_chart_casts(self):
        for name in ("trend", "recent_runs", "boots_for_run",
                     "latest_per_device"):
            with self.subTest(query=name):
                conn = RecordingConn()
                if name == "trend":
                    queries.trend(conn, "d1")
                elif name == "recent_runs":
                    queries.recent_runs(conn, "d1")
                elif name == "boots_for_run":
                    queries.boots_for_run(conn, 1)
                else:
                    queries.latest_per_device(conn)
                self.assertIn("::float8", conn.sql[0])


class Trend(unittest.TestCase):

    def test_pins_phase_and_boot_index(self):
        # The rmtfs mask means boot 1 and boot 3 of the same run are
        # different systems; mixing them produces a sawtooth that reads as a
        # real regression.
        conn = RecordingConn()
        queries.trend(conn, "d1")
        sql, params = conn.executed[0]
        self.assertIn("phase = %s", sql)
        self.assertIn("boot_index = %s", sql)
        self.assertEqual(params[:3], ["d1", "default", 1])

    def test_excludes_debug_cmdline_by_default(self):
        conn = RecordingConn()
        queries.trend(conn, "d1")
        self.assertIn("cmdline_has_debug IS NOT TRUE", conn.sql[0])

    def test_keeps_unverified_boots_in_the_series(self):
        # IS NOT TRUE, not = false: excluding every boot recorded before
        # cmdline capture existed would silently empty the chart, which
        # looks like "no data" rather than "filtered".
        conn = RecordingConn()
        queries.trend(conn, "d1")
        self.assertNotIn("cmdline_has_debug = false", conn.sql[0])

    def test_debug_phase_is_not_filtered_on_debug_cmdline(self):
        # A debug-phase boot is *supposed* to carry the debug params, so the
        # filter would exclude the entire series. The column is still
        # selected -- the UI shows it -- but nothing is filtered on it.
        conn = RecordingConn()
        queries.trend(conn, "d1", phase="debug")
        self.assertNotIn("AND cmdline_has_debug", conn.sql[0])

    def test_reflashed_filter_is_opt_in_and_three_valued(self):
        conn = RecordingConn()
        queries.trend(conn, "d1", reflashed_only=True)
        self.assertIn("reflashed IS NOT FALSE", conn.sql[0])

        plain = RecordingConn()
        queries.trend(plain, "d1")
        self.assertNotIn("reflashed IS NOT", plain.sql[0])

    def test_selects_the_newest_n_then_returns_them_oldest_first(self):
        # Ordering ASC and then LIMIT would chart the oldest N and never
        # move -- a chart that looks fine and is permanently stale.
        conn = RecordingConn(rows=[{"build_number": 3}, {"build_number": 2},
                                   {"build_number": 1}])
        rows = queries.trend(conn, "d1")
        self.assertIn("ORDER BY build_number DESC", conn.sql[0])
        self.assertEqual([r["build_number"] for r in rows], [1, 2, 3])

    def test_limit_is_the_last_parameter(self):
        conn = RecordingConn()
        queries.trend(conn, "d1", limit=7)
        self.assertEqual(conn.executed[0][1][-1], 7)

    def test_reads_the_view_not_the_tables(self):
        # boot_trend already filters to recorded boots and carries the run
        # context; re-deriving the join here is how the two drift.
        conn = RecordingConn()
        queries.trend(conn, "d1")
        self.assertIn("FROM boot_trend", conn.sql[0])


class DeviceUpsert(unittest.TestCase):

    def test_values_line_up_with_the_column_list(self):
        conn = RecordingConn()
        queries.upsert_device(conn, DEVICE)
        sql, params = conn.executed[0]
        self.assertEqual(len(params), len(schema.DEVICE_COLUMNS))
        self.assertEqual(sql.count("%s"), len(schema.DEVICE_COLUMNS))

    def test_slug_is_derived_not_configured(self):
        conn = RecordingConn()
        queries.upsert_device(conn, DEVICE)
        _sql, params = conn.executed[0]
        self.assertIn(DEVICE.slug, params)

    def test_refreshes_last_seen_but_never_first_seen(self):
        # "this board has been benchmarked since March" must survive a
        # config reload.
        conn = RecordingConn()
        queries.upsert_device(conn, DEVICE)
        sql, _params = conn.executed[0]
        self.assertIn("last_seen_at = now()", sql)
        self.assertNotIn("first_seen_at", sql)

    def test_updates_every_column_except_the_key(self):
        conn = RecordingConn()
        queries.upsert_device(conn, DEVICE)
        sql, _params = conn.executed[0]
        self.assertIn("ON CONFLICT (device_id) DO UPDATE", sql)
        self.assertNotIn("device_id = EXCLUDED.device_id", sql)
        for column in schema.DEVICE_COLUMNS:
            if column == "device_id":
                continue
            with self.subTest(column=column):
                self.assertIn(f"{column} = EXCLUDED.{column}", sql)


class Enqueue(unittest.TestCase):

    def test_expands_the_all_shorthand(self):
        conn = RecordingConn(rows=[(42,)])
        queries.enqueue_run(conn, DEVICE)
        _sql, params = conn.executed[0]
        self.assertIn(["flash", "capture", "report"], params)

    def test_honours_an_explicit_stage_list(self):
        conn = RecordingConn(rows=[(42,)])
        queries.enqueue_run(conn, DEVICE, stages="capture report")
        _sql, params = conn.executed[0]
        self.assertIn(["capture", "report"], params)

    def test_records_boots_expected_as_twice_num_boots(self):
        # The capture stage runs num_boots default boots then num_boots
        # debug boots, so a 3-boot device records 6.
        conn = RecordingConn(rows=[(42,)])
        queries.enqueue_run(conn, DEVICE)
        _sql, params = conn.executed[0]
        self.assertIn(6, params)

    def test_inserts_queued_not_running(self):
        conn = RecordingConn(rows=[(42,)])
        queries.enqueue_run(conn, DEVICE)
        self.assertIn("'queued'", conn.sql[0])

    def test_notifies_the_worker_in_the_same_transaction(self):
        # A rolled-back enqueue must not notify about a run that does not
        # exist, so there is no commit between the INSERT and the notify.
        conn = RecordingConn(rows=[(42,)])
        run_id = queries.enqueue_run(conn, DEVICE)
        self.assertEqual(run_id, 42)
        self.assertEqual(len(conn.executed), 2)
        self.assertIn("pg_notify('bootbench_jobs', %s)", conn.sql[1])
        self.assertEqual(conn.executed[1][1], ("42",))
        self.assertEqual(conn.commits, 0,
                         "enqueue must leave the commit to its caller")

    def test_build_fields_are_optional(self):
        conn = RecordingConn(rows=[(42,)])
        queries.enqueue_run(conn, DEVICE)
        _sql, params = conn.executed[0]
        self.assertEqual(params.count(None), 4 + 2)  # build x4, triggered, parent

    def test_build_fields_are_recorded_when_known(self):
        # The scheduler already asked the agent for the latest build to
        # decide whether to run at all, so the dashboard can name the build
        # of a queued run before the agent has done anything.
        conn = RecordingConn(rows=[(42,)])
        queries.enqueue_run(conn, DEVICE, build={
            "build_number": 2471, "build_folder": "..._master_2471",
            "build_path": r"\\share\x\performance", "share_root": r"\\share",
        })
        _sql, params = conn.executed[0]
        self.assertIn(2471, params)


class SetRunFields(unittest.TestCase):

    def test_rejects_a_column_that_does_not_exist(self):
        # Otherwise a typo is a silent no-op or a psycopg error far from its
        # cause.
        with self.assertRaises(ValueError) as ctx:
            queries.set_run_fields(RecordingConn(), 1, bots_recorded=6)
        self.assertIn("bots_recorded", str(ctx.exception))

    def test_accepts_every_ingest_column(self):
        for column in schema.RUN_INGEST_COLUMNS:
            with self.subTest(column=column):
                queries.set_run_fields(RecordingConn(), 1, **{column: None})

    def test_accepts_the_queue_side_transitions(self):
        for column in ("status", "agent_job_id", "last_output_at",
                       "cancel_requested", "acknowledged_at", "readopted"):
            with self.subTest(column=column):
                queries.set_run_fields(RecordingConn(), 1, **{column: None})

    def test_no_fields_is_a_no_op_not_a_malformed_update(self):
        conn = RecordingConn()
        queries.set_run_fields(conn, 1)
        self.assertEqual(conn.executed, [])

    def test_run_id_is_the_last_parameter(self):
        conn = RecordingConn()
        queries.set_run_fields(conn, 99, exit_code=0)
        sql, params = conn.executed[0]
        self.assertTrue(sql.endswith("WHERE run_id = %s"))
        self.assertEqual(params[-1], 99)


class FinishRun(unittest.TestCase):

    def test_stamps_finished_at_when_not_given(self):
        conn = RecordingConn()
        queries.finish_run(conn, 1, "success")
        self.assertIn("finished_at = now()", conn.sql[-1])

    def test_does_not_overwrite_an_existing_finished_at(self):
        # A re-adopted run finished when its status document says it did.
        conn = RecordingConn()
        queries.finish_run(conn, 1, "success")
        self.assertIn("finished_at IS NULL", conn.sql[-1])

    def test_an_explicit_finished_at_wins(self):
        when = datetime.datetime(2026, 10, 6, 1, 4)
        conn = RecordingConn()
        queries.finish_run(conn, 1, "success", finished_at=when)
        sql, params = conn.executed[0]
        self.assertIn("finished_at = %s", sql)
        self.assertIn(when, params)
        self.assertNotIn("now()", sql)


class SchedulerQuestions(unittest.TestCase):

    def test_an_unparseable_build_is_never_already_benchmarked(self):
        # build_number is NULL for any branch that is not a master nightly;
        # NULL = NULL is never true in SQL, so this has to short-circuit in
        # Python or every tick would re-run.
        conn = RecordingConn()
        self.assertFalse(queries.is_build_benchmarked(conn, "d1", None))
        self.assertEqual(conn.executed, [], "no query should be issued")

    def test_partial_counts_as_benchmarked(self):
        conn = RecordingConn(rows=[(1,)])
        self.assertTrue(queries.is_build_benchmarked(conn, "d1", 2471))
        _sql, params = conn.executed[0]
        self.assertIn("partial", params[-1])

    def test_active_run_covers_queued_and_running(self):
        conn = RecordingConn(rows=[{"run_id": 1}])
        queries.active_run(conn, "d1")
        _sql, params = conn.executed[0]
        self.assertEqual(sorted(params[-1]), ["queued", "running"])

    def test_claim_skips_rows_another_worker_holds(self):
        conn = RecordingConn(rows=[{"run_id": 7}])
        queries.claim_next_run(conn)
        self.assertIn("FOR UPDATE SKIP LOCKED", conn.sql[0])
        self.assertIn("ORDER BY queued_at", conn.sql[0])

    def test_claim_marks_running_and_stamps_started_at(self):
        conn = RecordingConn(rows=[{"run_id": 7}])
        queries.claim_next_run(conn)
        self.assertIn("status = 'running'", conn.sql[1])
        self.assertIn("started_at = now()", conn.sql[1])

    def test_an_empty_queue_claims_nothing(self):
        conn = RecordingConn(rows=[])
        self.assertIsNone(queries.claim_next_run(conn))
        self.assertEqual(len(conn.executed), 1, "no UPDATE on an empty queue")

    def test_the_device_lock_is_namespaced(self):
        # hashtext over a bare device_id could collide with any other
        # advisory lock in the database.
        conn = RecordingConn(rows=[(True,)])
        queries.try_lock_device(conn, "iq-9075-evk-01")
        _sql, params = conn.executed[0]
        self.assertEqual(params, ("bootbench:device:iq-9075-evk-01",))

    def test_lock_and_unlock_use_the_same_key(self):
        lock = RecordingConn(rows=[(True,)])
        unlock = RecordingConn(rows=[(True,)])
        queries.try_lock_device(lock, "d1")
        queries.unlock_device(unlock, "d1")
        self.assertEqual(lock.executed[0][1], unlock.executed[0][1])

    def test_consecutive_errors_resets_on_a_non_error_decision(self):
        # The "4 consecutive failed ticks" alert has to measure a persistent
        # condition, not a counter that only ever grows.
        conn = RecordingConn()
        queries.touch_scheduler_state(conn, "d1", action="skipped",
                                      reason="not_due", errored=False)
        sql, params = conn.executed[0]
        self.assertIn("ELSE 0 END", sql)
        self.assertIn(False, params)

    def test_errored_increments(self):
        conn = RecordingConn()
        queries.touch_scheduler_state(conn, "d1", action="error",
                                      reason="agent_unhealthy", errored=True)
        sql, params = conn.executed[0]
        self.assertIn("consecutive_errors + 1", sql)
        self.assertIn(True, params)


class EnumCasting(unittest.TestCase):
    """Enums reject a typo'd status at write time, which is why they are
    enums -- but a Python str arrives as `text`, and there is no
    `run_status = text` operator. Every placeholder bound to an enum column
    therefore carries an explicit cast. Without it the failure is a
    confusing "operator does not exist" from a path that only runs at
    01:00."""

    def test_status_assignment_is_cast(self):
        conn = RecordingConn()
        queries.set_run_fields(conn, 1, status="partial")
        self.assertIn("status = %s::run_status", conn.sql[0])

    def test_a_plain_text_column_is_not_cast(self):
        conn = RecordingConn()
        queries.set_run_fields(conn, 1, failure_stage="flash")
        self.assertIn("failure_stage = %s", conn.sql[0])
        self.assertNotIn("failure_stage = %s::", conn.sql[0])

    def test_trigger_source_is_cast_on_enqueue(self):
        conn = RecordingConn(rows=[(42,)])
        queries.enqueue_run(conn, DEVICE, trigger_source="manual")
        self.assertIn("%s::trigger_source", conn.sql[0])

    def test_decision_action_is_cast(self):
        conn = RecordingConn(rows=[(1,)])
        queries.record_decision(conn, "d1", "enqueued", "new_build")
        self.assertIn("%s::decision_action", conn.sql[0])

    def test_scheduler_state_action_is_cast(self):
        conn = RecordingConn()
        queries.touch_scheduler_state(conn, "d1", action="skipped",
                                      reason="not_due")
        self.assertIn("%s::decision_action", conn.sql[0])

    def test_status_array_comparisons_are_cast(self):
        # ANY(%s) with a Python list of str is text[], which cannot be
        # compared against a run_status column either.
        active = RecordingConn(rows=[{"run_id": 1}])
        queries.active_run(active, "d1")
        self.assertIn("ANY(%s::run_status[])", active.sql[0])

        probe = RecordingConn(rows=[(1,)])
        queries.is_build_benchmarked(probe, "d1", 2471)
        self.assertIn("ANY(%s::run_status[])", probe.sql[0])

    def test_every_enum_column_names_a_declared_type(self):
        declared = set(schema.parse_enums())
        for column, enum in queries.ENUM_COLUMNS.items():
            with self.subTest(column=column):
                self.assertIn(enum, declared)


class JsonSafety(unittest.TestCase):

    def test_sql_null_is_not_json_null(self):
        # Jsonb(None) stores the JSON value `null`, for which IS NULL is
        # false and jsonb_array_length raises. "no hitters computed" and
        # "hitters computed, empty" are different claims.
        self.assertIsNone(queries._jsonb(None))

    def test_decimal_is_converted_and_warned_about(self):
        with self.assertLogs("src.db.queries", level="WARNING") as logs:
            out = queries.json_safe({"total_multiuser_s":
                                     decimal.Decimal("12.481")})
        self.assertEqual(out, {"total_multiuser_s": 12.481})
        self.assertIn("::float8", logs.output[0])

    def test_timestamps_become_iso_strings(self):
        out = queries.json_safe(
            [{"started_at": datetime.datetime(2026, 10, 6, 1, 4)}])
        self.assertEqual(out[0]["started_at"], "2026-10-06T01:04:00")

    def test_nested_structures_are_walked(self):
        out = queries.json_safe(
            {"boots": [{"seconds": {"kernel": decimal.Decimal("5.9")}}]})
        self.assertEqual(out["boots"][0]["seconds"]["kernel"], 5.9)

    def test_ordinary_values_pass_through_untouched(self):
        payload = {"a": 1, "b": "x", "c": None, "d": True, "e": 1.5}
        self.assertEqual(queries.json_safe(payload), payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
