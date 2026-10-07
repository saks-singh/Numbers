"""Ingestion: the mapping from a status document to rows.

No Postgres. What is checked here is the part that is decision-making --
which status earns `partial`, which boot counts as displayed, what a NULL
`cmdline_has_debug` means, and whether a 2009-line Windows script's actual
output maps onto the column list. The SQL those rows travel in is pinned by
test_db_queries.py; that it executes is test_db_live.py.

The boot fixtures are real collect-script output, so `build_run` here is the
same code path the bench host runs.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _stub_psycopg_if_absent():
    """Let `queries._jsonb` run without a Postgres driver installed.

    `Jsonb` is a value wrapper -- it carries an object through to the
    adapter and does nothing else -- so standing in for it changes nothing
    this module asserts. psycopg is a genuine declared dependency of the
    server; it is just absent on a developer box with no Postgres, and
    refusing to run there would mean the mapping layer is only ever tested
    where the database already answers.
    """
    try:
        import psycopg  # noqa: F401
        return
    except ImportError:
        pass

    class Jsonb:
        def __init__(self, obj):
            self.obj = obj

        def __eq__(self, other):
            return isinstance(other, Jsonb) and other.obj == self.obj

        def __repr__(self):
            return f"Jsonb({self.obj!r})"

    psycopg = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    json_mod = types.ModuleType("psycopg.types.json")
    json_mod.Jsonb = Jsonb
    types_mod = types.ModuleType("psycopg.types")
    types_mod.json = json_mod
    psycopg.rows = rows
    psycopg.types = types_mod
    sys.modules.update({"psycopg": psycopg, "psycopg.rows": rows,
                        "psycopg.types": types_mod,
                        "psycopg.types.json": json_mod})


_stub_psycopg_if_absent()

from src.db import ingest, queries, schema  # noqa: E402
from src.utils import bootbench_api as bb  # noqa: E402

FIXTURES = HERE / "fixtures"
BOOT_LOGS = FIXTURES / "boot_logs"


def unwrap(value):
    """A JSONB-wrapped value as the plain object it carries."""
    return getattr(value, "obj", value)


class RecordingCursor:
    def __init__(self, conn):
        self.conn = conn
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.conn.statements.append((self.sql, params))

    def fetchone(self):
        # A bare `(1,)` for everything would be wrong in one place that
        # matters: backfill's dedupe probe reads a row as "already
        # ingested", so a stub that always finds one skips every run and the
        # test asserting boots were written fails with no hint why. SELECTs
        # find nothing unless a test says otherwise; INSERT ... RETURNING
        # hands back a fresh id so runs are distinguishable.
        if self.sql.startswith("SELECT"):
            return self.conn.select_result
        self.conn.next_id += 1
        return (self.conn.next_id,)

    def fetchall(self):
        return []

    @property
    def rowcount(self):
        return 0


class RecordingConn:
    """Records the statements ingestion would run, and answers them.

    Not a Postgres substitute and not trying to be: it asserts the mapping
    from a status document to columns and values. Whether those statements
    execute is tests/test_db_live.py, against a real server.
    """

    def __init__(self, select_result=None):
        self.statements = []
        self.commits = 0
        self.next_id = 100
        self.select_result = select_result

    def cursor(self, **_kwargs):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def _column_rows(self, prefix):
        rows = []
        for sql, params in self.statements:
            if sql.startswith(prefix):
                columns = sql.split("(", 1)[1].split(")", 1)[0]
                names = [c.strip() for c in columns.split(",")]
                rows.append({n: unwrap(v) for n, v in zip(names, params)})
        return rows

    def inserted_boots(self):
        """[{column: value}] for every INSERT INTO boot."""
        return self._column_rows("INSERT INTO boot (")

    def inserted_runs(self):
        return self._column_rows("INSERT INTO run (")

    def run_updates(self):
        """{column: value} merged across every UPDATE run."""
        merged = {}
        for sql, params in self.statements:
            if not sql.startswith("UPDATE run SET "):
                continue
            assignments = sql[len("UPDATE run SET "):].split(" WHERE ")[0]
            names = [a.split("=")[0].strip()
                     for a in assignments.split(", ")]
            merged.update((n, unwrap(v)) for n, v in zip(names, params))
        return merged


def status_doc(**overrides) -> dict:
    doc = {
        "schema_version": 1,
        "host": "BENCH-WIN-01",
        "target": "iq-9075-evk",
        "share_root": r"\\swayam\QLI_Builds\Yocto",
        "build_path": r"\\swayam\x\..._Nightly_Build_master_2471\performance",
        "build_folder": "qcom-multimedia-proprietary-image_Nightly_Build_master_2471",
        "build_number": 2471,
        "stages_requested": ["flash", "capture", "report"],
        "stages_run": ["flash", "capture", "report"],
        "exit_code": 0,
        "failure_stage": None,
        "error_class": None,
        "error_message": None,
        "boots_expected": 6,
        "boots_recorded": 6,
        "started_utc": "2026-10-06T01:04:11+00:00",
        "ended_utc": "2026-10-06T01:46:22+00:00",
        "outputs": {
            "data_json": r"C:\bench\artifacts\iq-9075-evk-01\Boot-Charts\d.json",
            "report_html": r"C:\bench\artifacts\iq-9075-evk-01\Boot-Charts\r.html",
            "boot_logs_dir": r"C:\bench\artifacts\iq-9075-evk-01\Boot-Logs",
        },
        "boots": [],
    }
    doc.update(overrides)
    return doc


def boot_entry(log_name, **overrides) -> dict:
    phase, _, index = log_name.rpartition("-")
    entry = {
        "log_name": log_name,
        "phase": phase,
        "boot_index": int(index),
        "timestamp": "2026-10-06 01:20",
        "parse_error": None,
        "kernel_cmdline": "console=ttyMSM0,115200n8 root=PARTUUID=abc rw",
        "cmdline_has_debug": False,
        "seconds": {key: 1.5 for key in schema.SECONDS_KEYS},
        "critical_chain": ["multi-user.target", "basic.target"],
        "overall_overheads": [{"unit": "rmtfs.service", "seconds": 0.9}],
        "hitters": {"kernel": [{"unit": "foo", "seconds": 0.4}]},
    }
    entry.update(overrides)
    return entry


def full_run(**overrides) -> dict:
    boots = [boot_entry(f"{phase}-{i}")
             for phase in ("default", "debug") for i in (1, 2, 3)]
    return status_doc(boots=boots, **overrides)


class Coercion(unittest.TestCase):

    def test_a_formatted_string_goes_through_the_skills_own_parser(self):
        # The fallback that makes pre-`seconds` history ingestible. It must
        # be the skill's rule, not a second regex: a subtly different one
        # produces a plausible number, and a plausible wrong number on a
        # trend chart is worse than a crash.
        self.assertEqual(ingest._sec("12.481 s"), 12.481)
        self.assertEqual(ingest._sec("12.481 s"), bb.parse_seconds("12.481 s"))

    def test_a_number_is_rounded_to_the_column_scale(self):
        # NUMERIC(10,3) would round on the way in anyway; doing it here
        # means the value asserted in a test is the value stored.
        self.assertEqual(ingest._sec(12.4814), 12.481)
        self.assertEqual(ingest._sec(12), 12.0)

    def test_unparseable_and_missing_both_become_null(self):
        for value in (None, "", "n/a", "--", "lots"):
            with self.subTest(value=value):
                self.assertIsNone(ingest._sec(value))

    def test_a_bool_is_not_a_number(self):
        # True is an int in Python, so `isinstance(v, int)` alone would
        # store 1.0 seconds for a flag that leaked into a metric slot.
        self.assertIsNone(ingest._sec(True))
        self.assertIsNone(ingest._sec(False))

    def test_a_naive_timestamp_is_read_as_utc(self):
        parsed = ingest._utc("2026-10-06T01:04:11")
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_a_z_suffix_parses(self):
        self.assertIsNotNone(ingest._utc("2026-10-06T01:04:11Z"))

    def test_a_bad_timestamp_costs_a_field_not_the_ingest(self):
        with self.assertLogs("src.db.ingest", level="WARNING"):
            self.assertIsNone(ingest._utc("sometime tuesday"))
        self.assertIsNone(ingest._utc(None))

    def test_stages_accepts_a_string_or_a_list(self):
        self.assertEqual(ingest._stages("flash capture"),
                         ["flash", "capture"])
        self.assertEqual(ingest._stages("flash,capture"),
                         ["flash", "capture"])
        self.assertEqual(ingest._stages(["flash"]), ["flash"])
        self.assertEqual(ingest._stages(None), [])


class LogNames(unittest.TestCase):
    """`build_run` sets neither phase nor boot_index -- pull_and_record
    attaches them afterwards -- so anything built from the skill's own report
    JSON has only the name to go on."""

    def test_the_index_comes_off_the_name(self):
        self.assertEqual(ingest.boot_index_from_log_name("default-2"), 2)
        self.assertEqual(ingest.boot_index_from_log_name("debug-3"), 3)

    def test_the_phase_comes_off_the_name(self):
        self.assertEqual(ingest.phase_from_log_name("default-2"), "default")
        self.assertEqual(ingest.phase_from_log_name("debug-1"), "debug")

    def test_an_unnumbered_name_yields_no_index(self):
        self.assertIsNone(ingest.boot_index_from_log_name("default"))
        self.assertIsNone(ingest.boot_index_from_log_name(None))


class DebugCmdline(unittest.TestCase):

    def test_an_unreadable_cmdline_is_unknown_not_clean(self):
        # The distinction the trend filter depends on: `IS NOT TRUE` keeps
        # unknowns, so boots recorded before the cmdline was read still
        # chart. `= false` would have emptied the chart instead.
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(ingest.cmdline_has_debug(value))

    def test_a_clean_cmdline_is_false(self):
        self.assertIs(ingest.cmdline_has_debug("console=ttyMSM0 root=x rw"),
                      False)

    def test_each_debug_param_the_skill_appends_is_detected(self):
        for param in ("initcall_debug", "systemd.log_level=debug",
                      "log_buf_len=4M"):
            with self.subTest(param=param):
                self.assertIs(
                    ingest.cmdline_has_debug(f"console=ttyMSM0 {param} rw"),
                    True)

    def test_the_rule_is_the_skills_own(self):
        # Asserted rather than trusted, because a divergence here silently
        # includes a debug-polluted boot in a default-phase trend -- and
        # systemd.log_level=debug measurably slows boot, so it reads as a
        # regression that no code change caused.
        skill = bb.get("cmdline_has_debug_params")
        for cmdline in ("console=x rw", "console=x initcall_debug rw",
                        "console=x log_buf_len=4M rw"):
            with self.subTest(cmdline=cmdline):
                self.assertEqual(ingest.cmdline_has_debug(cmdline),
                                 skill(cmdline))


class DerivedStatus(unittest.TestCase):
    """Exit code alone is not enough, and this is the distinction the whole
    system exists to make: the skill exits 0 having recorded five of six
    boots, because five beats none. A device quietly dropping a boot every
    night must not render as a plain success."""

    def test_all_boots_recorded_is_success(self):
        self.assertEqual(ingest.derive_status(
            status_doc(boots_recorded=6, boots_expected=6)), "success")

    def test_a_missing_boot_is_partial(self):
        self.assertEqual(ingest.derive_status(
            status_doc(boots_recorded=5, boots_expected=6)), "partial")

    def test_a_nonzero_exit_is_failed(self):
        self.assertEqual(ingest.derive_status(
            status_doc(exit_code=12, boots_recorded=0)), "failed")

    def test_exit_zero_with_nothing_recorded_is_failed(self):
        # Should be impossible. The report is treated as authoritative over
        # the exit code, because a success with no data is not a success.
        self.assertEqual(ingest.derive_status(
            status_doc(exit_code=0, boots_recorded=0)), "failed")

    def test_an_unknown_expectation_does_not_manufacture_partial(self):
        self.assertEqual(ingest.derive_status(
            status_doc(boots_expected=None, boots_recorded=2)), "success")


class RunFields(unittest.TestCase):

    def setUp(self):
        self.fields = ingest.run_fields_from_status(full_run())

    def test_every_field_is_a_declared_ingest_column(self):
        unknown = set(self.fields) - set(schema.RUN_INGEST_COLUMNS)
        self.assertEqual(unknown, set())

    def test_the_queue_side_is_never_touched(self):
        # An ingest that reset these would erase who asked for the run and
        # why -- the trigger source, the person, the parent retry.
        for column in ("status", "queued_at", "trigger_source",
                       "triggered_by", "parent_run_id", "device_id",
                       "cancel_requested", "acknowledged_at"):
            with self.subTest(column=column):
                self.assertNotIn(column, self.fields)

    def test_reflashed_follows_stages_run(self):
        self.assertIs(self.fields["reflashed"], True)
        capture_only = ingest.run_fields_from_status(
            full_run(stages_run=["capture", "report"]))
        self.assertIs(capture_only["reflashed"], False)

    def test_reflashed_is_why_the_nightly_must_flash(self):
        # The collect script masks rmtfs.service persistently, so a
        # capture-only run starts from a different system than the run
        # before it. This column is how the dashboard can exclude them.
        self.assertIn("reflashed", schema.RUN_INGEST_COLUMNS)

    def test_the_whole_document_is_kept_verbatim(self):
        # Anything this schema does not model stays recoverable without
        # going back to the bench host, which may have pruned it.
        self.assertEqual(self.fields["status_json"]["host"], "BENCH-WIN-01")

    def test_output_paths_are_recorded_as_the_agents_not_ours(self):
        self.assertTrue(self.fields["agent_data_json"].startswith(r"C:\bench"))
        self.assertTrue(
            self.fields["agent_boot_logs_dir"].startswith(r"C:\bench"))

    def test_timestamps_become_aware_datetimes(self):
        self.assertIsNotNone(self.fields["started_at"].tzinfo)
        self.assertIsNotNone(self.fields["finished_at"].tzinfo)


class BootRows(unittest.TestCase):

    def test_every_attempted_boot_gets_a_row(self):
        # All 2N, not the 2 the skill's report displays. That is the whole
        # reason this database exists.
        rows = ingest.boot_rows_from_status(full_run())
        self.assertEqual(len(rows), 6)
        self.assertEqual([r["log_name"] for r in rows],
                         ["default-1", "default-2", "default-3",
                          "debug-1", "debug-2", "debug-3"])

    def test_every_key_is_a_declared_boot_column(self):
        rows = ingest.boot_rows_from_status(full_run())
        unknown = set(rows[0]) - set(schema.BOOT_COLUMNS)
        self.assertEqual(unknown, set())

    def test_every_duration_column_is_populated(self):
        row = ingest.boot_rows_from_status(full_run())[0]
        for column in schema.SECONDS_COLUMNS:
            with self.subTest(column=column):
                self.assertEqual(row[column], 1.5)

    def test_a_failed_boot_is_recorded_as_a_row_not_dropped(self):
        # "This device dropped a boot on the 3rd" has to stay answerable.
        doc = full_run()
        doc["boots"][1] = boot_entry(
            "default-2", parse_error="RuntimeError: dmesg.txt not found",
            seconds={})
        rows = ingest.boot_rows_from_status(doc)
        self.assertEqual(len(rows), 6)
        failed = next(r for r in rows if r["log_name"] == "default-2")
        self.assertIs(failed["recorded"], False)
        self.assertIn("dmesg.txt", failed["parse_error"])
        self.assertIsNone(failed["total_multiuser_s"])

    def test_displayed_is_the_first_parsed_boot_of_each_phase(self):
        rows = ingest.boot_rows_from_status(full_run())
        self.assertEqual([r["log_name"] for r in rows if r["displayed"]],
                         ["default-1", "debug-1"])

    def test_displayed_skips_a_boot_that_failed_to_parse(self):
        # Mirrors _pick_displayed: with default-1 unparsed, the report shows
        # default-2. Getting this wrong would mark a row displayed that the
        # skill's own HTML does not show.
        doc = full_run()
        doc["boots"][0] = boot_entry("default-1", parse_error="boom",
                                     seconds={})
        rows = ingest.boot_rows_from_status(doc)
        self.assertEqual([r["log_name"] for r in rows if r["displayed"]],
                         ["default-2", "debug-1"])

    def test_the_displayed_rule_is_the_skills_own(self):
        doc = full_run()
        parsed = [b for b in doc["boots"] if not b.get("parse_error")]
        expected = [b["log_name"] for b in bb.pick_displayed(parsed)]
        rows = ingest.boot_rows_from_status(doc)
        self.assertEqual([r["log_name"] for r in rows if r["displayed"]],
                         expected)

    def test_seq_preserves_the_order_pulled(self):
        rows = ingest.boot_rows_from_status(full_run())
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3, 4, 5, 6])

    def test_phase_and_index_are_recovered_when_absent(self):
        doc = full_run()
        for entry in doc["boots"]:
            entry.pop("phase")
            entry.pop("boot_index")
        rows = ingest.boot_rows_from_status(doc)
        self.assertEqual([(r["phase"], r["boot_index"]) for r in rows[:3]],
                         [("default", 1), ("default", 2), ("default", 3)])

    def test_a_null_index_would_be_invisible_to_the_trend(self):
        # Why the recovery above exists rather than storing NULL: the trend
        # query pins a single boot_index, so a backfill that left it unset
        # would load history and chart nothing.
        sql = queries.trend.__doc__ or ""
        self.assertIn("boot_index", sql + str(queries.trend.__code__.co_consts))

    def test_the_skills_verdict_on_the_cmdline_wins_when_present(self):
        doc = full_run()
        doc["boots"][0]["cmdline_has_debug"] = True
        doc["boots"][0]["kernel_cmdline"] = "console=x rw"  # says otherwise
        rows = ingest.boot_rows_from_status(doc)
        self.assertIs(rows[0]["cmdline_has_debug"], True)

    def test_the_cmdline_is_read_when_the_skill_did_not_record_a_verdict(self):
        # What makes a document written before the field existed usable.
        doc = full_run()
        doc["boots"][0].pop("cmdline_has_debug")
        doc["boots"][0]["kernel_cmdline"] = "console=x initcall_debug rw"
        rows = ingest.boot_rows_from_status(doc)
        self.assertIs(rows[0]["cmdline_has_debug"], True)

    def test_empty_json_fields_become_null_not_empty_containers(self):
        # An empty list in JSONB is not NULL: `IS NULL` is false for it and
        # a UI branching on absence would render an empty table instead of
        # nothing.
        doc = full_run()
        doc["boots"][0]["hitters"] = {}
        doc["boots"][0]["overall_overheads"] = []
        rows = ingest.boot_rows_from_status(doc)
        self.assertIsNone(rows[0]["hitters"])
        self.assertIsNone(rows[0]["overall_overheads"])

    def test_no_boots_yields_no_rows_and_does_not_raise(self):
        self.assertEqual(ingest.boot_rows_from_status(status_doc()), [])


class IngestStatus(unittest.TestCase):

    def test_it_writes_the_run_then_every_boot(self):
        conn = RecordingConn()
        summary = ingest.ingest_status(conn, 42, full_run())
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["boots"], 6)
        self.assertEqual(summary["recorded"], 6)
        self.assertEqual(len(conn.inserted_boots()), 6)

    def test_it_does_not_commit(self):
        # The caller owns the commit: the worker finalizes the run status in
        # the same transaction, and boots that landed without their status
        # would be indistinguishable from a crashed ingest.
        conn = RecordingConn()
        ingest.ingest_status(conn, 42, full_run())
        self.assertEqual(conn.commits, 0)

    def test_every_boot_insert_upserts(self):
        # Idempotence is by design, not defensiveness: a run re-adopted
        # after an agent restart is ingested twice.
        conn = RecordingConn()
        ingest.ingest_status(conn, 42, full_run())
        for sql, _params in conn.statements:
            if sql.startswith("INSERT INTO boot"):
                self.assertIn("ON CONFLICT (run_id, log_name) DO UPDATE", sql)

    def test_the_run_id_reaches_every_boot(self):
        conn = RecordingConn()
        ingest.ingest_status(conn, 42, full_run())
        self.assertEqual({r["run_id"] for r in conn.inserted_boots()}, {42})

    def test_a_partial_run_reports_which_boots_failed(self):
        doc = full_run(boots_recorded=5)
        doc["boots"][1] = boot_entry("default-2", parse_error="boom",
                                     seconds={})
        conn = RecordingConn()
        summary = ingest.ingest_status(conn, 42, doc)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["parse_errors"], ["default-2"])

    def test_artifact_dir_is_recorded_when_given(self):
        conn = RecordingConn()
        ingest.ingest_status(conn, 42, full_run(),
                            artifact_dir="/var/lib/bootbench/runs/42")
        self.assertEqual(conn.run_updates()["artifact_dir"],
                         "/var/lib/bootbench/runs/42")

    def test_readopted_is_flagged_when_finalized_from_a_status_file(self):
        conn = RecordingConn()
        ingest.ingest_status(conn, 42, full_run(), readopted=True)
        self.assertIs(conn.run_updates()["readopted"], True)

    def test_a_newer_schema_version_warns_and_ingests_anyway(self):
        # Refusing would discard a real night's data over a field nobody
        # reads. The fields this module reads are additive.
        conn = RecordingConn()
        with self.assertLogs("src.db.ingest", level="WARNING") as captured:
            summary = ingest.ingest_status(conn, 42, full_run(
                schema_version=ingest.SUPPORTED_SCHEMA_VERSION + 1))
        self.assertEqual(summary["boots"], 6)
        self.assertIn("newer", "\n".join(captured.output))

    def test_a_missing_schema_version_warns(self):
        conn = RecordingConn()
        doc = full_run()
        doc.pop("schema_version")
        with self.assertLogs("src.db.ingest", level="WARNING"):
            ingest.ingest_status(conn, 42, doc)

    def test_a_non_object_document_is_refused(self):
        with self.assertRaises(ingest.IngestError):
            ingest.ingest_status(RecordingConn(), 42, ["not", "a", "doc"])


class Backfill(unittest.TestCase):
    """The skill's own per-target JSON. Its two limits are inherent to the
    source: it holds 2 of the 2N boots, and its timestamps are naive local
    minute stamps."""

    def target_json(self, *, with_seconds=True, runs=2) -> Path:
        golden = json.loads(
            (FIXTURES / "golden" / "boots.json").read_text(encoding="utf-8"))
        boots = golden if isinstance(golden, list) else golden["runs"][0]["boots"]
        entries = []
        for index in range(runs):
            copied = []
            for boot in boots[:2]:
                boot = json.loads(json.dumps(boot))
                if not with_seconds:
                    # History recorded before build_run emitted numbers.
                    boot.pop("seconds", None)
                copied.append(boot)
            entries.append({
                "timestamp": f"2026-10-0{index + 1} 01:04",
                "target": "iq-9075-evk",
                "build_path": (r"\\swayam\x\qcom-image_Nightly_Build_master_"
                               f"{2460 + index}\\performance"),
                "boots": copied,
            })
        path = Path(self.tmp.name) / "bootchart-data-iq9075evk.json"
        path.write_text(json.dumps({"runs": entries}), encoding="utf-8")
        return path

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_each_entry_becomes_a_run_with_boots(self):
        conn = RecordingConn()
        results = ingest.ingest_from_target_json(
            conn, "iq-9075-evk-01", self.target_json(), target="iq-9075-evk")
        self.assertEqual(len(results), 2)
        self.assertEqual(len(conn.inserted_boots()), 4)

    def test_the_build_number_is_parsed_out_of_the_path(self):
        conn = RecordingConn()
        ingest.ingest_from_target_json(
            conn, "iq-9075-evk-01", self.target_json(runs=1))
        inserts = [p for sql, p in conn.statements
                   if sql.startswith("INSERT INTO run (")]
        self.assertIn(2460, inserts[0])

    def test_history_without_the_seconds_key_still_ingests(self):
        # The parse_seconds fallback, which is the reason run_metrics_seconds
        # exists. Without it every run recorded before Phase 1 would land
        # with sixteen NULLs.
        conn = RecordingConn()
        ingest.ingest_from_target_json(
            conn, "iq-9075-evk-01", self.target_json(with_seconds=False,
                                                     runs=1))
        row = conn.inserted_boots()[0]
        self.assertIsNotNone(row["total_multiuser_s"])
        self.assertGreater(row["total_multiuser_s"], 0)

    def test_the_report_metrics_match_whether_or_not_seconds_was_recorded(self):
        # The two paths must agree on the six metrics the report displays,
        # or the chart steps at the point in history where `seconds` was
        # introduced -- a discontinuity that looks exactly like a real
        # regression and is caused only by an ingest detail.
        with_key, without = RecordingConn(), RecordingConn()
        ingest.ingest_from_target_json(
            with_key, "d", self.target_json(runs=1))
        ingest.ingest_from_target_json(
            without, "d", self.target_json(with_seconds=False, runs=1))
        for key in schema.METRIC_KEYS:
            column = f"{key}_s"
            with self.subTest(column=column):
                value = with_key.inserted_boots()[0][column]
                self.assertIsNotNone(value)
                self.assertEqual(value,
                                 without.inserted_boots()[0][column])

    def test_subcomponents_are_null_in_history_predating_the_seconds_key(self):
        # Not a defect to fix: `firmware`, `loader`, `userspace` and the
        # rest were never emitted as numbers, only interpolated into prose
        # `note` strings. The fallback recovers the six formatted
        # METRIC_ROWS values and nothing else, so these columns are NULL
        # for old runs and a chart over them must tolerate gaps rather than
        # read a missing sub-component as zero.
        conn = RecordingConn()
        ingest.ingest_from_target_json(
            conn, "d", self.target_json(with_seconds=False, runs=1))
        row = conn.inserted_boots()[0]
        unavailable = [f"{k}_s" for k in schema.SECONDS_KEYS
                       if k not in schema.METRIC_KEYS]
        for column in unavailable:
            with self.subTest(column=column):
                self.assertIsNone(row[column])

    def test_backfilled_runs_carry_the_backfill_trigger_source(self):
        conn = RecordingConn()
        ingest.ingest_from_target_json(
            conn, "iq-9075-evk-01", self.target_json(runs=1))
        inserts = [sql for sql, _p in conn.statements
                   if sql.startswith("INSERT INTO run (")]
        self.assertIn("'backfill'::trigger_source", inserts[0])

    def test_a_re_run_probes_for_what_it_already_ingested(self):
        # Backfill is run by hand, more than once. The skill's naive
        # timestamp is the only identity the file offers.
        conn = RecordingConn()
        ingest.ingest_from_target_json(
            conn, "iq-9075-evk-01", self.target_json(runs=1))
        probes = [p for sql, p in conn.statements
                  if "backfilled_from" in sql and sql.startswith("SELECT")]
        self.assertTrue(probes)
        self.assertIn("2026-10-01 01:04", probes[0])

    def test_an_already_backfilled_run_is_skipped_not_duplicated(self):
        conn = RecordingConn(select_result=(77,))
        results = ingest.ingest_from_target_json(
            conn, "iq-9075-evk-01", self.target_json(runs=2))
        self.assertEqual([r["status"] for r in results],
                         ["skipped", "skipped"])
        self.assertEqual(conn.inserted_boots(), [])

    def test_reflashed_is_left_unknown_rather_than_guessed(self):
        # Unknowable from this file, and the column is nullable for exactly
        # this. `False` would be a different claim, and a wrong one:
        # trend(reflashed_only=True) filters `IS NOT FALSE`, so guessing
        # would drop every backfilled run from the chart.
        conn = RecordingConn()
        ingest.ingest_from_target_json(
            conn, "iq-9075-evk-01", self.target_json(runs=1))
        self.assertIsNone(conn.run_updates()["reflashed"])

    def test_limit_takes_the_newest_entries(self):
        conn = RecordingConn()
        results = ingest.ingest_from_target_json(
            conn, "d", self.target_json(runs=2), limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["bootbench_ts"], "2026-10-02 01:04")

    def test_metric_notes_survive_this_path_only(self):
        # The status document reports on the job and deliberately omits the
        # report's prose; the report JSON has it.
        conn = RecordingConn()
        ingest.ingest_from_target_json(
            conn, "d", self.target_json(runs=1))
        self.assertIsNotNone(conn.inserted_boots()[0]["metric_notes"])

    def test_a_file_without_a_runs_list_is_refused(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text('{"nope": 1}', encoding="utf-8")
        with self.assertRaises(ingest.IngestError):
            ingest.ingest_from_target_json(RecordingConn(), "d", path)


class Reparse(unittest.TestCase):
    """The repair path: re-read pulled logs with the skill's own parsers.
    Deliberately not the primary path -- it redoes work the bench host
    already did, and the two can silently disagree."""

    def test_it_reparses_every_boot_directory_present(self):
        conn = RecordingConn()
        summary = ingest.reparse_boot_logs(conn, 42, BOOT_LOGS)
        self.assertEqual(summary["boots"], 6)
        self.assertEqual(summary["recorded"], 6)
        self.assertEqual(summary["parse_errors"], [])

    def test_the_numbers_come_from_the_skills_own_build_run(self):
        conn = RecordingConn()
        ingest.reparse_boot_logs(conn, 42, BOOT_LOGS)
        row = next(r for r in conn.inserted_boots()
                   if r["log_name"] == "default-1")

        texts = bb.get("read_pulled_logs")(BOOT_LOGS, "default-1")
        run = bb.get("build_run")(
            None, None, texts["sat_text"], texts["cc_text"],
            texts["cc_sysinit_text"], texts["blame_text"],
            texts["dmesg_text"], texts["journalctl_text"])
        expected = bb.run_metrics_seconds(run)
        self.assertEqual(row["total_multiuser_s"],
                         round(expected["total_multiuser"], 3))

    def test_it_reads_the_cmdline_the_skills_parsers_never_did(self):
        # The collect script has always written kernel_cmdline.txt and
        # read_pulled_logs is the only thing that reads it. This is the
        # cheap safeguard against a 'default' boot that really ran debug-y.
        conn = RecordingConn()
        ingest.reparse_boot_logs(conn, 42, BOOT_LOGS)
        row = conn.inserted_boots()[0]
        self.assertIsNotNone(row["kernel_cmdline"])
        self.assertIn(row["cmdline_has_debug"], (True, False))

    def test_it_deletes_before_rewriting(self):
        # A log directory since renamed would otherwise leave an orphan row
        # the UPSERT never reaches.
        conn = RecordingConn()
        ingest.reparse_boot_logs(conn, 42, BOOT_LOGS)
        self.assertEqual(conn.statements[0][0],
                         "DELETE FROM boot WHERE run_id = %s")

    def test_it_does_not_rewrite_the_runs_verdict(self):
        # A successful reparse does not retroactively make a failed flash
        # succeed; only the boot counts are corrected.
        conn = RecordingConn()
        ingest.reparse_boot_logs(conn, 42, BOOT_LOGS)
        updates = conn.run_updates()
        self.assertEqual(set(updates),
                         {"boots_recorded", "boots_expected"})

    def test_one_unreadable_boot_does_not_lose_the_others(self):
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "logs"
            shutil.copytree(BOOT_LOGS, staged)
            (staged / "Logs-default-2" / "dmesg.txt").unlink()

            conn = RecordingConn()
            summary = ingest.reparse_boot_logs(conn, 42, staged)

        self.assertEqual(summary["recorded"], 5)
        self.assertEqual(summary["parse_errors"], ["default-2"])
        failed = next(r for r in conn.inserted_boots()
                      if r["log_name"] == "default-2")
        self.assertIs(failed["recorded"], False)
        self.assertIn("dmesg.txt", failed["parse_error"])

    def test_a_directory_with_no_boots_is_an_error(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ingest.IngestError):
                ingest.reparse_boot_logs(RecordingConn(), 42, tmp)

    def test_a_missing_directory_is_an_error(self):
        with self.assertRaises(ingest.IngestError):
            ingest.reparse_boot_logs(RecordingConn(), 42, "/no/such/dir")


class SkillSurface(unittest.TestCase):
    """The shim. Its value is that there is one implementation of 'what does
    12.481 s mean', and its risk is reaching for something that touches
    hardware."""

    def test_the_skill_loads_on_this_machine(self):
        self.assertTrue(bb.source().is_file())

    def test_a_device_touching_function_is_refused(self):
        for name in ("cmd_flash", "run_pcat_flash", "open_console",
                     "save_data", "render", "discover_latest_build"):
            with self.subTest(name=name):
                with self.assertRaises(RuntimeError):
                    bb.get(name)

    def test_every_forbidden_name_actually_exists_in_the_skill(self):
        # Otherwise the list rots into a set of typos that forbid nothing.
        module = bb.load()
        missing = sorted(n for n in bb.FORBIDDEN if not hasattr(module, n))
        self.assertEqual(missing, [], f"not in bootbench.py: {missing}")

    def test_a_missing_function_names_the_drift(self):
        with self.assertRaises(AttributeError) as ctx:
            bb.get("parse_something_that_never_existed")
        self.assertIn("out of step", str(ctx.exception))

    def test_the_exit_taxonomy_is_imported_not_restated(self):
        codes = bb.exit_codes()
        self.assertEqual(codes["EXIT_OK"], 0)
        self.assertEqual(codes["EXIT_FLASH"], 12)
        self.assertEqual(codes["EXIT_INTERRUPTED"], 130)

    def test_the_metric_order_is_the_reports_order(self):
        self.assertEqual(bb.metric_keys(), schema.METRIC_KEYS)

    def test_a_missing_skill_names_what_it_tried(self):
        # The one failure mode of a path-based import that a person has to
        # debug from the message alone.
        from unittest import mock

        with mock.patch.object(bb, "CANDIDATES", ()), \
                mock.patch.dict(os.environ, {bb.ENV_VAR: ""}, clear=False):
            with self.assertRaises(FileNotFoundError) as ctx:
                bb.find_bootbench("/definitely/not/here/bootbench.py")
        message = str(ctx.exception)
        self.assertIn("BOOTBENCH_SKILL_PATH", message)
        self.assertIn("config/server.yaml", message)
        # The configured path is echoed back, so a typo in it is visible
        # without having to read this module.
        self.assertIn("definitely", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
