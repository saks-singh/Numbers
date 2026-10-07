"""The parts only a real Postgres can answer.

Skipped unless BOOTBENCH_TEST_DSN is set, because the coordinator's target
is a Linux server and this repository is edited on a Windows laptop with no
Postgres. Everything checkable without a server lives in
`test_schema_consistency.py`, `test_db_migrate.py` and `test_db_queries.py`;
what is left here is the set of claims that are only true if the server
agrees:

  * the DDL is valid SQL and applies to an empty database
  * it applies a second time without error, with and without its own
    bookkeeping rows
  * NUMERIC(10,3) round-trips a 3-decimal second exactly
  * the chart queries hand back floats, so json.dumps does not raise
  * the enums reject a typo
  * FOR UPDATE SKIP LOCKED really does give two workers different rows
  * the partial indexes are usable by the queries written for them

Run it against a throwaway database:

    createdb bootbench_test
    BOOTBENCH_TEST_DSN=postgresql:///bootbench_test \\
        python -m unittest tests.test_db_live -v
"""

from __future__ import annotations

import decimal
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.db import migrate, queries, schema  # noqa: E402
from src.inventory import Device  # noqa: E402

DSN = os.environ.get("BOOTBENCH_TEST_DSN")
REASON = "set BOOTBENCH_TEST_DSN to a throwaway database to run these"

DEVICE = Device(
    device_id="live-test-01",
    target="iq-9075-evk",
    agent_url="http://bench-win-01:8765",
    com_port="COM7", tac_port="VTP8", adb_serial="1a2b3c4d5e",
    num_boots=3,
)


def _connect():
    import psycopg

    return psycopg.connect(DSN, autocommit=False)


def setUpModule():
    if not DSN:
        raise unittest.SkipTest(REASON)
    with _connect() as conn:
        migrate.apply_migrations(conn)
        conn.commit()


@unittest.skipUnless(DSN, REASON)
class LiveCase(unittest.TestCase):
    """One connection per test, rolled back at the end, so the tests are
    order-independent and leave the database as they found it."""

    def setUp(self):
        self.conn = _connect()
        self.addCleanup(self.conn.close)
        self.addCleanup(self.conn.rollback)
        queries.upsert_device(self.conn, DEVICE)

    def insert_run(self, *, status="success", build_number=2471,
                   boots_recorded=6, device_id=DEVICE.device_id):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO run (device_id, target, build_number, status, "
                "                 boots_expected, boots_recorded, started_at) "
                "VALUES (%s, %s, %s, %s::run_status, 6, %s, now()) "
                "RETURNING run_id",
                (device_id, DEVICE.target, build_number, status,
                 boots_recorded),
            )
            (run_id,) = cur.fetchone()
        return run_id

    def insert_boot(self, run_id, *, log_name="default-1", phase="default",
                    boot_index=1, recorded=True, seconds=None,
                    cmdline_has_debug=False):
        values = {c: None for c in schema.BOOT_COLUMNS}
        values.update(
            run_id=run_id, log_name=log_name, phase=phase,
            boot_index=boot_index, seq=1, displayed=True, recorded=recorded,
            cmdline_has_debug=cmdline_has_debug,
        )
        for key, value in (seconds or {}).items():
            values[schema.seconds_column(key)] = value
        columns = list(schema.BOOT_COLUMNS)
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO boot ({', '.join(columns)}) "
                f"VALUES ({', '.join(['%s'] * len(columns))}) "
                f"RETURNING boot_id",
                [values[c] for c in columns],
            )
            (boot_id,) = cur.fetchone()
        return boot_id


class Migrations(LiveCase):

    def test_a_second_apply_skips_everything(self):
        actions = migrate.apply_migrations(self.conn)
        self.assertTrue(actions)
        self.assertTrue(all(a == "skipped" for _n, a, _d in actions),
                        [(n, a) for n, a, _d in actions])

    def test_nothing_is_pending_or_changed_after_applying(self):
        state = migrate.status(self.conn)
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["changed"], [])

    def test_the_ddl_is_idempotent_without_its_own_bookkeeping(self):
        # Belt and braces: the bookkeeping table is created by the migration
        # that would have to record it, so the DDL has to survive a re-run
        # on a populated database on its own merits.
        sql = (schema.MIGRATIONS_DIR / "001_init.sql").read_text(
            encoding="utf-8")
        with self.conn.cursor() as cur:
            cur.execute(sql)  # must not raise against a populated database

    def test_every_expected_relation_actually_exists(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'")
            present = {name for (name,) in cur.fetchall()}
        expected = set(schema.parse_tables()) | set(schema.parse_views())
        self.assertTrue(expected <= present, sorted(expected - present))

    def test_the_ddl_columns_match_the_live_table(self):
        # The parser is checked against the file; this checks the file
        # against the server, which closes the loop.
        with self.conn.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'boot'")
            live = {name for (name,) in cur.fetchall()}
        self.assertEqual(live, set(schema.parse_tables()["boot"]))


class NumericPrecision(LiveCase):

    def test_three_decimal_seconds_round_trip_exactly(self):
        # The whole reason for NUMERIC over DOUBLE PRECISION: a
        # build-over-build delta must not be able to produce -0.000.
        run_id = self.insert_run()
        self.insert_boot(run_id, seconds={"total_multiuser": "12.481"})
        with self.conn.cursor() as cur:
            cur.execute("SELECT total_multiuser_s FROM boot WHERE run_id = %s",
                         (run_id,))
            (value,) = cur.fetchone()
        self.assertEqual(value, decimal.Decimal("12.481"))

    def test_a_delta_of_identical_values_is_exactly_zero(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT (12.481::numeric(10,3) - 12.481::numeric(10,3))")
            (delta,) = cur.fetchone()
        self.assertEqual(delta, decimal.Decimal("0.000"))
        self.assertEqual(f"{delta:+.3f}", "+0.000")


class JsonSerialisation(LiveCase):
    """Risk 9, asserted against the server rather than reasoned about: a
    Decimal reaching json.dumps raises deep inside Flask's serializer, where
    the traceback does not name the column."""

    def test_trend_rows_are_json_serialisable(self):
        run_id = self.insert_run()
        self.insert_boot(run_id, seconds={"total_multiuser": "12.481",
                                          "kernel": "5.912"})
        rows = queries.trend(self.conn, DEVICE.device_id)
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0]["total_multiuser_s"], float)
        json.dumps(queries.json_safe(rows))  # must not raise

    def test_every_duration_comes_back_as_a_float(self):
        run_id = self.insert_run()
        self.insert_boot(run_id, seconds={k: "1.500"
                                          for k in schema.SECONDS_KEYS})
        rows = queries.trend(self.conn, DEVICE.device_id)
        for column in schema.SECONDS_COLUMNS:
            with self.subTest(column=column):
                self.assertIsInstance(rows[0][column], float)

    def test_boots_for_run_is_json_serialisable(self):
        run_id = self.insert_run()
        self.insert_boot(run_id, seconds={"total_multiuser": "12.481"})
        rows = queries.boots_for_run(self.conn, run_id)
        json.dumps(queries.json_safe(rows))

    def test_recent_runs_is_json_serialisable(self):
        run_id = self.insert_run()
        self.insert_boot(run_id, seconds={"total_multiuser": "12.481"})
        rows = queries.recent_runs(self.conn, DEVICE.device_id)
        json.dumps(queries.json_safe(rows))

    def test_latest_per_device_is_json_serialisable(self):
        run_id = self.insert_run()
        self.insert_boot(run_id, seconds={"total_multiuser": "12.481"})
        rows = queries.latest_per_device(self.conn)
        json.dumps(queries.json_safe(rows))


class Constraints(LiveCase):

    def test_an_unknown_status_is_rejected_at_write_time(self):
        # The point of an enum over TEXT + CHECK.
        import psycopg

        with self.assertRaises(psycopg.errors.InvalidTextRepresentation):
            self.insert_run(status="succeeded")

    def test_re_ingesting_the_same_boot_conflicts(self):
        # UNIQUE (run_id, log_name) is what makes ingestion idempotent: a
        # re-adopted run and a reparse repair both ingest the same run.
        import psycopg

        run_id = self.insert_run()
        self.insert_boot(run_id, log_name="default-1")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.insert_boot(run_id, log_name="default-1")

    def test_the_same_log_name_in_a_different_run_is_fine(self):
        first = self.insert_run()
        second = self.insert_run()
        self.insert_boot(first, log_name="default-1")
        self.insert_boot(second, log_name="default-1")

    def test_deleting_a_run_takes_its_boots(self):
        run_id = self.insert_run()
        self.insert_boot(run_id)
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM run WHERE run_id = %s", (run_id,))
            cur.execute("SELECT count(*) FROM boot WHERE run_id = %s",
                        (run_id,))
            (remaining,) = cur.fetchone()
        self.assertEqual(remaining, 0)

    def test_a_run_needs_a_real_device(self):
        import psycopg

        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.insert_run(device_id="not-in-the-inventory")

    def test_cmdline_has_debug_is_three_valued(self):
        run_id = self.insert_run()
        for index, value in enumerate((True, False, None), start=1):
            with self.subTest(cmdline_has_debug=value):
                self.insert_boot(run_id, log_name=f"default-{index}",
                                 boot_index=index, cmdline_has_debug=value)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM boot WHERE run_id = %s "
                        "AND cmdline_has_debug IS NULL", (run_id,))
            (unknown,) = cur.fetchone()
        self.assertEqual(unknown, 1, "NULL must mean 'not known'")

    def test_first_seen_at_survives_a_config_reload(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT first_seen_at FROM device WHERE device_id = %s",
                        (DEVICE.device_id,))
            (before,) = cur.fetchone()
        queries.upsert_device(self.conn, DEVICE)
        with self.conn.cursor() as cur:
            cur.execute("SELECT first_seen_at FROM device WHERE device_id = %s",
                        (DEVICE.device_id,))
            (after,) = cur.fetchone()
        self.assertEqual(before, after)


class Views(LiveCase):

    def test_run_latest_is_the_newest_run_whatever_its_state(self):
        self.insert_run(status="success", build_number=2468)
        newest = self.insert_run(status="failed", build_number=2471)
        with self.conn.cursor() as cur:
            cur.execute("SELECT run_id FROM run_latest WHERE device_id = %s",
                        (DEVICE.device_id,))
            rows = cur.fetchall()
        self.assertEqual(rows, [(newest,)])

    def test_boot_trend_excludes_unparsed_boots(self):
        run_id = self.insert_run()
        self.insert_boot(run_id, log_name="default-1", recorded=True)
        self.insert_boot(run_id, log_name="default-2", boot_index=2,
                         recorded=False)
        with self.conn.cursor() as cur:
            cur.execute("SELECT log_name FROM boot_trend WHERE run_id = %s",
                        (run_id,))
            rows = {name for (name,) in cur.fetchall()}
        self.assertEqual(rows, {"default-1"})

    def test_trend_pins_boot_index_one(self):
        # boot 1 follows a fresh flash; boots 2 and 3 run with rmtfs masked
        # and are a different system. Mixing them is a sawtooth that reads
        # as a regression.
        run_id = self.insert_run()
        self.insert_boot(run_id, log_name="default-1", boot_index=1,
                         seconds={"total_multiuser": "12.481"})
        self.insert_boot(run_id, log_name="default-2", boot_index=2,
                         seconds={"total_multiuser": "9.100"})
        rows = queries.trend(self.conn, DEVICE.device_id)
        self.assertEqual([r["total_multiuser_s"] for r in rows], [12.481])

    def test_trend_drops_a_default_boot_that_really_ran_debug(self):
        run_id = self.insert_run()
        self.insert_boot(run_id, log_name="default-1", cmdline_has_debug=True,
                         seconds={"total_multiuser": "14.000"})
        self.assertEqual(queries.trend(self.conn, DEVICE.device_id), [])

    def test_trend_keeps_a_boot_whose_cmdline_is_unknown(self):
        run_id = self.insert_run()
        self.insert_boot(run_id, log_name="default-1", cmdline_has_debug=None,
                         seconds={"total_multiuser": "12.481"})
        self.assertEqual(len(queries.trend(self.conn, DEVICE.device_id)), 1)

    def test_trend_returns_oldest_first(self):
        for build in (2465, 2468, 2471):
            run_id = self.insert_run(build_number=build)
            self.insert_boot(run_id, seconds={"total_multiuser": "12.000"})
        rows = queries.trend(self.conn, DEVICE.device_id)
        self.assertEqual([r["build_number"] for r in rows],
                         [2465, 2468, 2471])


class Queue(LiveCase):

    def test_enqueue_is_visible_as_an_active_run(self):
        run_id = queries.enqueue_run(self.conn, DEVICE)
        active = queries.active_run(self.conn, DEVICE.device_id)
        self.assertEqual(active["run_id"], run_id)
        self.assertEqual(active["status"], "queued")

    def test_claim_marks_it_running(self):
        run_id = queries.enqueue_run(self.conn, DEVICE)
        claimed = queries.claim_next_run(self.conn)
        self.assertEqual(claimed["run_id"], run_id)
        self.assertEqual(claimed["status"], "running")
        self.assertIsNotNone(claimed["started_at"])

    def test_claim_takes_the_oldest_first(self):
        first = queries.enqueue_run(self.conn, DEVICE)
        queries.enqueue_run(self.conn, DEVICE)
        self.assertEqual(queries.claim_next_run(self.conn)["run_id"], first)

    def test_an_empty_queue_claims_nothing(self):
        self.assertIsNone(queries.claim_next_run(self.conn))

    def test_finish_run_stamps_finished_at(self):
        run_id = queries.enqueue_run(self.conn, DEVICE)
        queries.finish_run(self.conn, run_id, "success", exit_code=0)
        row = queries.run_detail(self.conn, run_id)
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["exit_code"], 0)
        self.assertIsNotNone(row["finished_at"])

    def test_stages_requested_is_a_real_array(self):
        run_id = queries.enqueue_run(self.conn, DEVICE)
        row = queries.run_detail(self.conn, run_id)
        self.assertEqual(row["stages_requested"],
                         ["flash", "capture", "report"])


@unittest.skipUnless(DSN, REASON)
class TwoWorkers(unittest.TestCase):
    """Needs committed rows and two connections, so it manages its own
    lifecycle rather than using LiveCase's rollback."""

    def setUp(self):
        self.a = _connect()
        self.b = _connect()
        self.addCleanup(self.b.close)
        self.addCleanup(self.a.close)
        queries.upsert_device(self.a, DEVICE)
        self.run_ids = [queries.enqueue_run(self.a, DEVICE) for _ in range(2)]
        self.a.commit()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.a.rollback()
        self.b.rollback()
        with self.a.cursor() as cur:
            cur.execute("DELETE FROM run WHERE device_id = %s",
                        (DEVICE.device_id,))
            cur.execute("DELETE FROM device WHERE device_id = %s",
                        (DEVICE.device_id,))
        self.a.commit()

    def test_skip_locked_gives_each_worker_a_different_row(self):
        # Without SKIP LOCKED, worker B blocks on A's row instead of taking
        # the other one -- or, worse, both claim the same run.
        first = queries.claim_next_run(self.a)
        second = queries.claim_next_run(self.b)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual({first["run_id"], second["run_id"]},
                         set(self.run_ids))

    def test_the_device_lock_is_exclusive_across_connections(self):
        self.assertTrue(queries.try_lock_device(self.a, DEVICE.device_id))
        self.assertFalse(queries.try_lock_device(self.b, DEVICE.device_id))
        queries.unlock_device(self.a, DEVICE.device_id)
        self.assertTrue(queries.try_lock_device(self.b, DEVICE.device_id))
        queries.unlock_device(self.b, DEVICE.device_id)

    def test_different_devices_do_not_contend(self):
        self.assertTrue(queries.try_lock_device(self.a, "device-one"))
        self.assertTrue(queries.try_lock_device(self.b, "device-two"))
        queries.unlock_device(self.a, "device-one")
        queries.unlock_device(self.b, "device-two")


class SchedulerQuestions(LiveCase):

    def test_success_counts_as_benchmarked(self):
        self.insert_run(status="success", build_number=2471)
        self.assertTrue(
            queries.is_build_benchmarked(self.conn, DEVICE.device_id, 2471))

    def test_partial_counts_as_benchmarked(self):
        self.insert_run(status="partial", build_number=2471, boots_recorded=5)
        self.assertTrue(
            queries.is_build_benchmarked(self.conn, DEVICE.device_id, 2471))

    def test_a_failed_run_does_not_count(self):
        self.insert_run(status="failed", build_number=2471, boots_recorded=0)
        self.assertFalse(
            queries.is_build_benchmarked(self.conn, DEVICE.device_id, 2471))

    def test_consecutive_errors_accumulate_then_reset(self):
        for _ in range(3):
            queries.touch_scheduler_state(
                self.conn, DEVICE.device_id, action="error",
                reason="agent_unhealthy", errored=True)
        state = queries.get_scheduler_state(self.conn, DEVICE.device_id)
        self.assertEqual(state["consecutive_errors"], 3)

        queries.touch_scheduler_state(
            self.conn, DEVICE.device_id, action="skipped", reason="not_due")
        state = queries.get_scheduler_state(self.conn, DEVICE.device_id)
        self.assertEqual(state["consecutive_errors"], 0)
        self.assertEqual(state["last_action"], "skipped")

    def test_a_decision_is_recorded_for_every_outcome(self):
        # Including the boring ones: this is what answers "why didn't it run
        # last night?", which run rows alone cannot, because the whole point
        # is that no run was created.
        queries.record_decision(self.conn, DEVICE.device_id, "skipped",
                                "already_benchmarked", build_number=2471)
        rows = queries.recent_decisions(self.conn, DEVICE.device_id)
        self.assertEqual(rows[0]["reason"], "already_benchmarked")
        self.assertEqual(rows[0]["action"], "skipped")

    def test_decision_detail_round_trips_as_jsonb(self):
        queries.record_decision(self.conn, DEVICE.device_id, "error",
                                "discovery_failed",
                                detail={"error": "timed out", "attempt": 2})
        rows = queries.recent_decisions(self.conn, DEVICE.device_id)
        self.assertEqual(rows[0]["detail"]["attempt"], 2)

    def test_a_null_detail_is_sql_null_not_json_null(self):
        queries.record_decision(self.conn, DEVICE.device_id, "skipped",
                                "not_due")
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM scheduler_decision "
                        "WHERE device_id = %s AND detail IS NULL",
                        (DEVICE.device_id,))
            (nulls,) = cur.fetchone()
        self.assertEqual(nulls, 1)


class IndexUsage(LiveCase):
    """`enable_seqscan = off` because a test table is small enough that the
    planner would sequentially scan it regardless. These assert the index is
    *usable* for the query it was written for -- which is what catches a
    predicate that does not match the partial index's WHERE clause -- not
    that the planner picks it at this row count."""

    def explain(self, sql, params):
        with self.conn.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute(f"EXPLAIN {sql}", params)
            return "\n".join(line for (line,) in cur.fetchall())

    def test_the_benchmarked_probe_can_use_its_partial_index(self):
        plan = self.explain(
            "SELECT 1 FROM run WHERE device_id = %s AND build_number = %s "
            "AND status = ANY(%s::run_status[]) LIMIT 1",
            (DEVICE.device_id, 2471, list(queries.BENCHMARKED_STATUSES)),
        )
        self.assertIn("run_device_build_idx", plan, plan)

    def test_the_active_run_check_can_use_its_partial_index(self):
        plan = self.explain(
            "SELECT run_id FROM run WHERE device_id = %s "
            "AND status = ANY(%s::run_status[]) ORDER BY queued_at LIMIT 1",
            (DEVICE.device_id, list(queries.ACTIVE_STATUSES)),
        )
        self.assertIn("run_active_idx", plan, plan)

    def test_the_trend_history_scan_can_use_its_index(self):
        plan = self.explain(
            "SELECT run_id FROM run WHERE device_id = %s "
            "ORDER BY started_at DESC LIMIT 30",
            (DEVICE.device_id,),
        )
        self.assertIn("run_device_started_idx", plan, plan)

    def test_boots_for_run_can_use_its_index(self):
        plan = self.explain("SELECT boot_id FROM boot WHERE run_id = %s", (1,))
        self.assertIn("boot_run_idx", plan, plan)

    def test_the_overheads_search_can_use_the_gin_index(self):
        # "which runs mention this service at all" is how you chase a
        # regression back to the build that introduced it.
        plan = self.explain(
            "SELECT boot_id FROM boot WHERE overall_overheads @> %s",
            (queries._jsonb([{"unit": "rmtfs.service"}]),),
        )
        self.assertIn("boot_overheads_gin_idx", plan, plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
