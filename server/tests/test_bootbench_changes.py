"""Unit tests for the Phase 1 automation changes to `bootbench.py`.

These live here rather than in `boot-skills/` because the skill bundle has
no tests directory and is stdlib-only by design. The skill is still the
thing under test; this file just imports it.

`test_bootbench_golden.py` is the other half of the net: it proves the
*output* did not move. This file proves the *new behaviour* is real --
that the status document survives every way the script can terminate, that
each stage maps to its own exit code, and above all that one unparseable
boot no longer discards an hour of booting.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fixtures"))

import make_golden  # noqa: E402  (also puts the skill's scripts/ on sys.path)

import bootbench as bb  # noqa: E402


def tmpdir(prefix="bootbench-test-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class FakeArgs:
    """A stand-in for the argparse namespace.

    Deliberately not a Mock: `_run` reads ~15 attributes and a Mock would
    hand back a truthy Mock for every one of them, so a test could pass
    while the real CLI was missing the flag entirely.
    """

    def __init__(self, **kw):
        defaults = dict(
            stage=["latest-build"], target=None, com_port=None,
            boot_timeout=480, tac_port=None, dry_run=False, yes=True,
            skip_edl=False, recover=False, build_path=None, num_boots=None,
            resume_pull=False, report_cmd="render", run_json=None,
            json=False, json_status=None, adb_serial=None,
            boot_charts_dir=None, boot_logs_dir=None, share_root=None,
            non_interactive=True, lock_file=None, cancel_file=None,
            revert_debug_cmdline=False,
        )
        defaults.update(kw)
        for key, value in defaults.items():
            setattr(self, key, value)


# ---------------------------------------------------------------------------
# Exit-code taxonomy
# ---------------------------------------------------------------------------

class TestStageTagging(unittest.TestCase):
    """`_stage` is the only thing standing between ~30 untouched
    `raise RuntimeError` sites and a useful exit code."""

    def test_runtime_error_is_tagged_with_the_stage_code(self):
        with self.assertRaises(bb.BootbenchError) as ctx:
            with bb._stage("flash", bb.EXIT_FLASH):
                raise RuntimeError("PCAT fell over")
        self.assertEqual(ctx.exception.exit_code, bb.EXIT_FLASH)
        self.assertEqual(ctx.exception.stage, "flash")
        # The human-visible text must survive untouched -- the whole premise
        # is that only the exit code changes.
        self.assertEqual(str(ctx.exception), "PCAT fell over")

    def test_an_inner_tag_is_not_overwritten_by_an_outer_one(self):
        # cmd_flash nests build_discovery inside nothing today, but
        # capture wraps pull_and_record inside boot_collect. The specific
        # inner code is the useful one.
        with self.assertRaises(bb.BootbenchError) as ctx:
            with bb._stage("boot_collect", bb.EXIT_BOOT_COLLECT):
                with bb._stage("adb", bb.EXIT_ADB):
                    raise RuntimeError("adb pull failed")
        self.assertEqual(ctx.exception.exit_code, bb.EXIT_ADB)
        self.assertEqual(ctx.exception.stage, "adb")

    def test_a_non_runtime_error_is_left_alone(self):
        # A TypeError is a bug, not a known failure mode. Tagging it with a
        # stage code would disguise it as an expected hardware failure and
        # make the scheduler retry it forever.
        with self.assertRaises(TypeError):
            with bb._stage("flash", bb.EXIT_FLASH):
                raise TypeError("this is a bug")

    def test_bootbench_error_subclasses_runtime_error(self):
        # Load-bearing: existing `except RuntimeError` blocks in the script
        # must keep catching these.
        self.assertTrue(issubclass(bb.BootbenchError, RuntimeError))
        self.assertTrue(issubclass(bb.CancelledError, bb.BootbenchError))

    def test_every_exit_code_is_distinct_and_named(self):
        codes = {name: getattr(bb, name) for name in dir(bb)
                 if name.startswith("EXIT_")
                 and isinstance(getattr(bb, name), int)}
        self.assertEqual(len(codes), len(set(codes.values())), codes)
        for code in codes.values():
            self.assertIn(code, bb.EXIT_NAMES)


# ---------------------------------------------------------------------------
# The status document
# ---------------------------------------------------------------------------

class TestStatusDocument(unittest.TestCase):
    """The agent polls this file and a restarted agent finalizes a job from
    it, so "it exists afterwards" is a correctness requirement, not a
    nicety. It is written from a finally block for exactly that reason."""

    def setUp(self):
        self.root = tmpdir()
        self.path = self.root / "nested" / "status.json"

    def run_main(self, side_effect, argv_extra=()):
        argv = ["bootbench.py", "latest-build", "--json-status", str(self.path),
                *argv_extra]
        with patch.object(sys, "argv", argv), \
             patch.object(bb, "_run", side_effect=side_effect):
            with self.assertRaises(SystemExit) as ctx:
                bb.main()
        self.assertTrue(self.path.is_file(), "no status document was written")
        return ctx.exception.code, json.loads(self.path.read_text(encoding="utf-8"))

    def test_written_on_success(self):
        code, doc = self.run_main(lambda *a: bb.EXIT_OK)
        self.assertEqual(code, 0)
        self.assertEqual(doc["exit_code"], 0)
        self.assertIsNone(doc["error_class"])
        self.assertIsNotNone(doc["ended_utc"])

    def test_written_on_a_tagged_failure(self):
        err = bb.BootbenchError("no EDL device", bb.EXIT_EDL, "edl")
        code, doc = self.run_main(err)
        self.assertEqual(code, bb.EXIT_EDL)
        self.assertEqual(doc["exit_code"], bb.EXIT_EDL)
        self.assertEqual(doc["failure_stage"], "edl")
        self.assertEqual(doc["error_class"], "BootbenchError")
        self.assertEqual(doc["error_message"], "no EDL device")

    def test_written_on_keyboard_interrupt(self):
        # KeyboardInterrupt is not an Exception, so this needs its own
        # clause -- and Ctrl-C mid-run is exactly when you most want the
        # partial record.
        code, doc = self.run_main(KeyboardInterrupt())
        self.assertEqual(code, bb.EXIT_INTERRUPTED)
        self.assertEqual(doc["exit_code"], bb.EXIT_INTERRUPTED)
        self.assertEqual(doc["error_class"], "KeyboardInterrupt")

    def test_written_on_an_unexpected_exception(self):
        code, doc = self.run_main(ZeroDivisionError("bug"))
        self.assertEqual(code, bb.EXIT_UNKNOWN)
        self.assertEqual(doc["error_class"], "ZeroDivisionError")

    def test_written_on_a_usage_error(self):
        # parser.error() raises SystemExit, which skips every `except
        # Exception` clause. Without a dedicated clause the document would
        # claim exit 1 while the process really exited 2.
        code, doc = self.run_main(SystemExit(2))
        self.assertEqual(code, 2)
        self.assertEqual(doc["exit_code"], bb.EXIT_USAGE)
        self.assertEqual(doc["failure_stage"], "usage")

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        self.run_main(lambda *a: bb.EXIT_OK)
        siblings = [p.name for p in self.path.parent.iterdir()]
        self.assertEqual(siblings, ["status.json"], siblings)

    def test_a_failure_to_write_never_masks_the_real_error(self):
        # Pointing --json-status at a directory makes the write fail. The
        # run's own exit code must still be the one that matters.
        bb.status_init(FakeArgs())
        bb.status_set(exit_code=bb.EXIT_FLASH)
        bb.write_status(self.root)  # a directory, not a file
        self.assertTrue(self.root.is_dir())

    def test_stage_history_is_separate_from_pipeline_stages(self):
        # `stages_run` means flash/capture/report; `stage_history` means the
        # fine-grained failure labels. Merging them would make
        # failure_stage ambiguous.
        bb.status_init(FakeArgs())
        bb.status_set(stages_run=["capture"])
        bb.status_stage("adb")
        self.assertEqual(bb._STATUS["stages_run"], ["capture"])
        self.assertEqual(bb._STATUS["stage_history"], ["adb"])

    def test_status_set_cannot_blank_an_established_value(self):
        bb.status_init(FakeArgs())
        bb.status_set(target="iq-9075-evk")
        bb.status_set(target=None)
        self.assertEqual(bb._STATUS["target"], "iq-9075-evk")


# ---------------------------------------------------------------------------
# adb routing
# ---------------------------------------------------------------------------

class TestAdbRouting(unittest.TestCase):
    def tearDown(self):
        bb.ADB_SERIAL = None

    def test_no_serial_means_a_bare_adb_command(self):
        self.assertEqual(bb._adb_cmd("shell", "ls"), ["adb", "shell", "ls"])

    def test_an_explicit_serial_wins_over_the_global(self):
        bb.ADB_SERIAL = "global-serial"
        self.assertEqual(
            bb._adb_cmd("root", adb_serial="explicit"),
            ["adb", "-s", "explicit", "root"],
        )

    def test_the_global_is_used_when_no_explicit_serial_is_given(self):
        bb.ADB_SERIAL = "global-serial"
        self.assertEqual(bb._adb_cmd("root"),
                         ["adb", "-s", "global-serial", "root"])

    def test_wait_for_adb_device_rejects_a_different_serial(self):
        # The bug this covers: the old code accepted the FIRST ready line,
        # so on a two-board host it could silently pull logs off the wrong
        # device and record them against this one's build.
        out = "List of devices attached\notherboard\tdevice\n"
        with patch.object(bb.subprocess, "run") as run, \
             patch.object(bb.time, "sleep"):
            run.return_value = bb.subprocess.CompletedProcess(
                [], 0, stdout=out, stderr="")
            with self.assertRaises(RuntimeError) as ctx:
                bb.wait_for_adb_device(timeout_s=0, adb_serial="mine")
        message = str(ctx.exception)
        self.assertIn("mine", message)
        self.assertIn("otherboard", message, "the error should say what it saw")

    def test_wait_for_adb_device_accepts_the_matching_serial(self):
        out = "List of devices attached\notherboard\tdevice\nmine\tdevice\n"
        with patch.object(bb.subprocess, "run") as run, \
             patch.object(bb.time, "sleep"):
            run.return_value = bb.subprocess.CompletedProcess(
                [], 0, stdout=out, stderr="")
            bb.wait_for_adb_device(timeout_s=5, adb_serial="mine")

    def test_remote_log_names_sort_numerically_within_a_phase(self):
        # "default-10" must not sort between 1 and 2, or boots get pulled
        # and recorded in an order that misrepresents which boot was which.
        listing = "\n".join(f"/data/Logs-default-{i}" for i in (1, 2, 10, 3))
        with patch.object(bb.subprocess, "run") as run:
            run.return_value = bb.subprocess.CompletedProcess(
                [], 0, stdout=listing, stderr="")
            names = bb.adb_list_remote_log_names()
        self.assertEqual(names, ["default-1", "default-2", "default-3",
                                 "default-10"])


# ---------------------------------------------------------------------------
# Partial results -- the headline reliability fix
# ---------------------------------------------------------------------------

class TestPartialResults(unittest.TestCase):
    """One bad log file used to discard the entire run.

    `_read_pulled_file` raised, and that propagated through
    `read_pulled_logs` -> `build_run` -> `pull_and_record` with nothing at
    all recorded -- throwing away six boots' worth of flashing and
    rebooting because one file was short. These tests pin the new
    behaviour: the bad boot is recorded as a parse_error and the rest are
    kept.
    """

    def setUp(self):
        make_golden.write_fixtures()
        self.root = tmpdir()
        self.charts = self.root / "Boot-Charts"
        self.logs = self.root / "Boot-Logs"
        bb.status_init(FakeArgs())
        bb.apply_path_overrides(FakeArgs(
            boot_charts_dir=str(self.charts), boot_logs_dir=str(self.logs)))
        self.addCleanup(bb.apply_path_overrides, FakeArgs(
            boot_charts_dir=str(self._original_charts()),
            boot_logs_dir=str(self._original_logs())))

    _ORIGINAL = {}

    @classmethod
    def setUpClass(cls):
        cls._ORIGINAL = {"charts": bb.BOOT_CHARTS_DIR,
                         "logs": bb.BOOT_LOGS_DIR}

    def _original_charts(self):
        return self._ORIGINAL["charts"]

    def _original_logs(self):
        return self._ORIGINAL["logs"]

    def pull_with(self, damage=None):
        """Run pull_and_record against the fixture tree, optionally breaking
        one boot's logs first. adb is stubbed to a directory copy."""
        def fake_pull(local_dir, log_names, *, adb_serial=None):
            local_dir = Path(local_dir)
            local_dir.mkdir(parents=True, exist_ok=True)
            for name in log_names:
                src = make_golden.FIXTURES / f"Logs-{name}"
                dst = local_dir / f"Logs-{name}"
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            if damage:
                damage(local_dir)

        with patch.object(bb, "wait_for_adb_device"), \
             patch.object(bb, "adb_pull_logs", side_effect=fake_pull):
            return bb.pull_and_record("iq-9075-evk", make_golden.BUILD_PATH, 3)

    def test_a_healthy_run_records_every_boot(self):
        self.pull_with()
        self.assertEqual(bb._STATUS["boots_expected"], 6)
        self.assertEqual(bb._STATUS["boots_recorded"], 6)
        self.assertFalse(bb._STATUS["partial"])

    def test_one_missing_dmesg_leaves_five_boots_recorded_and_partial(self):
        def damage(local_dir):
            (local_dir / "Logs-default-2" / "dmesg.txt").unlink()

        self.pull_with(damage)

        self.assertEqual(bb._STATUS["boots_recorded"], 5)
        self.assertTrue(bb._STATUS["partial"])

        failed = [b for b in bb._STATUS["boots"] if b["parse_error"]]
        self.assertEqual([b["log_name"] for b in failed], ["default-2"])
        self.assertIn("dmesg", failed[0]["parse_error"])

        # And the five good boots are really in the report, not just counted.
        data = json.loads(
            (self.charts / "bootchart-data-iq9075evk.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["runs"]), 1)

    def test_the_displayed_boots_survive_losing_the_first_boot_of_a_phase(self):
        # The old code indexed boots[0] and boots[num_boots]. With
        # default-1 unparseable, boots[0] would be default-2 and
        # boots[num_boots] would land in the wrong phase entirely.
        def damage(local_dir):
            (local_dir / "Logs-default-1" / "dmesg.txt").unlink()

        self.pull_with(damage)
        data = json.loads(
            (self.charts / "bootchart-data-iq9075evk.json").read_text(encoding="utf-8"))
        displayed = data["runs"][0]["boots"]
        self.assertEqual([b["log_name"] for b in displayed],
                         ["default-2", "debug-1"])
        self.assertEqual([b["phase"] for b in displayed], ["default", "debug"])

    def test_every_boot_unparseable_still_raises(self):
        def damage(local_dir):
            for d in local_dir.iterdir():
                (d / "dmesg.txt").unlink()

        with self.assertRaises(bb.BootbenchError) as ctx:
            self.pull_with(damage)
        # The message must say where the logs are, or the operator has
        # nothing to inspect.
        self.assertIn("kept at", str(ctx.exception))
        self.assertEqual(bb._STATUS["boots_recorded"], 0)
        # EXIT_PARSE, not the enclosing stage's EXIT_ADB. The difference is
        # load-bearing for a scheduler: EXIT_ADB advertises "the logs are on
        # the device, retry with --resume-pull", and this failure is
        # deterministic -- that retry would burn another hour and fail
        # identically.
        self.assertEqual(ctx.exception.exit_code, bb.EXIT_PARSE)
        self.assertEqual(ctx.exception.stage, "parse")

    def test_every_boots_parse_error_is_recorded_before_the_run_fails(self):
        # The point of raising late rather than on the first failure: an
        # operator needs all six reasons, not just the exit code.
        def damage(local_dir):
            for d in local_dir.iterdir():
                (d / "dmesg.txt").unlink()

        with self.assertRaises(bb.BootbenchError):
            self.pull_with(damage)
        self.assertEqual(len(bb._STATUS["boots"]), 6)
        self.assertTrue(all(b["parse_error"] for b in bb._STATUS["boots"]))

    def test_boots_with_no_recognised_phase_fail_as_parse_not_indexerror(self):
        # Reachable through --resume-pull, where the phase names come from
        # whatever /data/Logs-* directories are on the device. Previously
        # _pick_displayed returned [] and displayed_boots[0] surfaced as a
        # bare IndexError traceback under EXIT_UNKNOWN.
        def fake_pull(local_dir, log_names, *, adb_serial=None):
            local_dir = Path(local_dir)
            local_dir.mkdir(parents=True, exist_ok=True)
            for name in log_names:
                shutil.copytree(make_golden.FIXTURES / "Logs-default-1",
                                local_dir / f"Logs-{name}")

        with patch.object(bb, "wait_for_adb_device"), \
             patch.object(bb, "adb_pull_logs", side_effect=fake_pull), \
             patch.object(bb, "adb_list_remote_log_names",
                          return_value=["scratch-1", "scratch-2"]):
            with self.assertRaises(bb.BootbenchError) as ctx:
                bb.pull_and_record("iq-9075-evk", make_golden.BUILD_PATH, None)

        self.assertEqual(ctx.exception.exit_code, bb.EXIT_PARSE)
        self.assertEqual(ctx.exception.stage, "parse")
        # Both boots parsed fine -- the failure is that neither is
        # displayable, and the message has to say that rather than claim a
        # parse failure that did not happen.
        self.assertEqual(bb._STATUS["boots_recorded"], 2)
        self.assertIn("scratch", str(ctx.exception))

    def test_each_boot_records_the_cmdline_it_actually_ran_with(self):
        # The safeguard against the never-reverted debug cmdline: a boot
        # labelled "default" that really ran with initcall_debug is
        # detectable rather than silently polluting the trend.
        self.pull_with()
        by_name = {b["log_name"]: b for b in bb._STATUS["boots"]}
        self.assertFalse(by_name["default-1"]["cmdline_has_debug"])
        self.assertTrue(by_name["debug-1"]["cmdline_has_debug"])
        self.assertIn("initcall_debug", by_name["debug-1"]["kernel_cmdline"])


class TestPickDisplayed(unittest.TestCase):
    def boots(self, *names):
        return [{"log_name": n, "phase": n.rpartition("-")[0]} for n in names]

    def test_picks_the_first_boot_of_each_phase(self):
        got = bb._pick_displayed(self.boots(
            "default-1", "default-2", "default-3", "debug-1", "debug-2"))
        self.assertEqual([b["log_name"] for b in got], ["default-1", "debug-1"])

    def test_a_single_phase_yields_a_single_boot(self):
        got = bb._pick_displayed(self.boots("default-1", "default-2"))
        self.assertEqual([b["log_name"] for b in got], ["default-1"])

    def test_phase_order_is_fixed_not_insertion_order(self):
        got = bb._pick_displayed(self.boots("debug-1", "default-1"))
        self.assertEqual([b["phase"] for b in got], ["default", "debug"])

    def test_no_boots_yields_nothing_rather_than_an_indexerror(self):
        self.assertEqual(bb._pick_displayed([]), [])


# ---------------------------------------------------------------------------
# Path overrides
# ---------------------------------------------------------------------------

class TestPathOverrides(unittest.TestCase):
    """These flags exist to move artifacts out of OneDrive, and they were
    silently ineffective: three functions bound the module constant as a
    default argument, which Python evaluates once at `def` time."""

    def setUp(self):
        self.original = (bb.BOOT_CHARTS_DIR, bb.BOOT_LOGS_DIR, bb.YOCTO_SHARE,
                         bb.DEFAULT_DATA, bb.DEFAULT_HTML)
        self.root = tmpdir()

    def tearDown(self):
        (bb.BOOT_CHARTS_DIR, bb.BOOT_LOGS_DIR, bb.YOCTO_SHARE,
         bb.DEFAULT_DATA, bb.DEFAULT_HTML) = self.original

    def test_overriding_the_charts_dir_recomputes_the_derived_paths(self):
        bb.apply_path_overrides(FakeArgs(boot_charts_dir=str(self.root)))
        self.assertEqual(bb.BOOT_CHARTS_DIR, self.root)
        # DEFAULT_DATA/DEFAULT_HTML are derived from it, so leaving them
        # pointing into the old directory would send the report back to
        # OneDrive while the logs went elsewhere.
        self.assertEqual(bb.DEFAULT_DATA.parent, self.root)
        self.assertEqual(bb.DEFAULT_HTML.parent, self.root)

    def test_boot_logs_run_dir_honors_the_override(self):
        bb.apply_path_overrides(FakeArgs(boot_logs_dir=str(self.root)))
        got = bb.boot_logs_run_dir("iq-9075-evk", make_golden.BUILD_PATH)
        self.assertEqual(got.parents[1], self.root)

    def test_save_run_backup_writes_under_the_override(self):
        # The end-to-end proof for the late-binding fix: a default argument
        # evaluated at def time meant this kept writing to the old path no
        # matter what the flag said.
        make_golden.write_fixtures()
        boot = make_golden.build_boots()[0]
        bb.apply_path_overrides(FakeArgs(boot_logs_dir=str(self.root)))
        written = bb.save_run_backup("iq-9075-evk", boot)
        self.assertTrue(str(written).startswith(str(self.root)), written)
        self.assertTrue(Path(written).is_file())

    def test_render_honors_the_override(self):
        make_golden.write_fixtures()
        bb.apply_path_overrides(FakeArgs(boot_charts_dir=str(self.root)))
        data_path, html_path = bb._data_html_paths("iq-9075-evk")
        self.assertEqual(data_path.parent, self.root)
        bb.save_data({"device": "iq-9075-evk", "runs": []}, data_path)
        bb.render()  # no arguments at all -- the defaults must re-resolve
        self.assertTrue(bb.DEFAULT_HTML.is_file())

    def test_share_root_override(self):
        bb.apply_path_overrides(FakeArgs(share_root=r"\\host\share"))
        self.assertEqual(bb.YOCTO_SHARE, r"\\host\share")

    def test_omitting_every_override_changes_nothing(self):
        bb.apply_path_overrides(FakeArgs())
        self.assertEqual(
            (bb.BOOT_CHARTS_DIR, bb.BOOT_LOGS_DIR, bb.YOCTO_SHARE,
             bb.DEFAULT_DATA, bb.DEFAULT_HTML),
            self.original,
        )


# ---------------------------------------------------------------------------
# Build discovery
# ---------------------------------------------------------------------------

class TestBuildDiscovery(unittest.TestCase):
    LISTING = "\n".join([
        "qcom-multimedia-proprietary-image_Nightly_Build_master_999",
        "qcom-multimedia-proprietary-image_Nightly_Build_master_1234",
        "qcom-multimedia-proprietary-image_Nightly_Build_master_1000",
        "some-unrelated-folder",
        "qcom-multimedia-proprietary-image_Nightly_Build_release_5000",
    ])

    def test_the_newest_build_is_chosen_numerically_not_lexically(self):
        # Lexically "999" > "1234". Getting this wrong means benchmarking a
        # months-old build every night and never noticing.
        with patch.object(bb, "_run_powershell", return_value=self.LISTING):
            number, folder = bb.find_latest_build_name(r"\\share")
        self.assertEqual(number, 1234)
        self.assertIn("1234", folder)

    def test_non_master_builds_are_invisible(self):
        with patch.object(bb, "_run_powershell", return_value=self.LISTING):
            number, _ = bb.find_latest_build_name(r"\\share")
        self.assertNotEqual(number, 5000)

    def test_find_latest_build_keeps_its_original_contract(self):
        # A drive-style root, not a UNC one: pathlib treats "\\\\share" as a
        # bare UNC root whose .name is empty, which would make this assert
        # something about pathlib rather than about the function.
        with patch.object(bb, "_run_powershell", return_value=self.LISTING):
            path = bb.find_latest_build(r"C:\builds")
        self.assertIsInstance(path, Path)
        self.assertIn("1234", path.name)

    def test_build_number_is_recovered_from_a_performance_suffixed_path(self):
        # cmd_flash hands capture the shared `...\performance` directory, so
        # the number has to be found past that suffix.
        path = (r"\\swayam\QLI_Builds\Yocto"
                r"\qcom-multimedia-proprietary-image_Nightly_Build_master_2471"
                r"\performance")
        self.assertEqual(bb.parse_build_number(path), 2471)

    def test_an_unparseable_build_path_yields_none_not_an_exception(self):
        # Branch and release builds are legitimately unparseable; the
        # scheduler reports `unparseable_build` rather than crashing.
        self.assertIsNone(bb.parse_build_number(r"\\share\some-other-build"))
        self.assertIsNone(bb.parse_build_number(None))

    def test_discovery_reports_image_readiness_instead_of_raising(self):
        # Step 7 of the scheduler's decision table depends on telling "no
        # new build" apart from "new build exists, this target's image
        # hasn't landed yet" -- the latter is normal and must not look like
        # a failure.
        with patch.object(bb, "_run_powershell", return_value=self.LISTING), \
             patch.object(bb, "resolve_build_dir",
                          side_effect=RuntimeError("missing rawprogram0.xml")):
            info = bb.discover_latest_build(r"\\share", "iq-9075-evk")
        self.assertFalse(info["target_image_ready"])
        self.assertEqual(info["build_number"], 1234)

    def test_discovery_without_a_target_does_not_probe_for_an_image(self):
        with patch.object(bb, "_run_powershell", return_value=self.LISTING), \
             patch.object(bb, "resolve_build_dir") as resolve:
            info = bb.discover_latest_build(r"\\share")
        resolve.assert_not_called()
        self.assertIsNone(info["target"])


# ---------------------------------------------------------------------------
# Numeric metrics
# ---------------------------------------------------------------------------

class TestNumericSeconds(unittest.TestCase):
    """The `seconds` key is what makes Postgres possible. It must agree with
    the string a human reads, exactly -- a numeric mirror that disagrees
    with the report is worse than no mirror at all."""

    @classmethod
    def setUpClass(cls):
        make_golden.write_fixtures()
        cls.boots = make_golden.build_boots()

    def test_seconds_mirrors_the_formatted_metric_values(self):
        for boot in self.boots:
            for key, *_ in bb.METRIC_ROWS:
                shown = boot["metrics"][key]["value"]
                self.assertEqual(
                    boot["seconds"][key], bb.parse_seconds(shown),
                    f"{boot['log_name']}:{key} shows {shown!r}",
                )

    def test_no_float_noise_in_any_value(self):
        # round() and format() can disagree at a half-way case, which is how
        # 5.035 became 5.034999999999999 next to a displayed "5.035 s".
        for boot in self.boots:
            for key, value in boot["seconds"].items():
                if value is None:
                    continue
                self.assertEqual(value, float(f"{value:.3f}"),
                                 f"{boot['log_name']}:{key} = {value!r}")

    def test_the_sub_components_are_exposed(self):
        # These existed only inside prose note/source strings before, which
        # is why the sub-component columns were impossible to populate.
        seconds = self.boots[0]["seconds"]
        for key in ("firmware", "loader", "userspace", "sat_total",
                    "init_exec", "epoch_advanced", "systemd_running",
                    "sysinit_target", "cc_multiuser"):
            self.assertIn(key, seconds)

    def test_run_metrics_seconds_prefers_the_stored_key(self):
        boot = self.boots[0]
        self.assertEqual(bb.run_metrics_seconds(boot), boot["seconds"])

    def test_run_metrics_seconds_falls_back_for_pre_change_history(self):
        # Backfilling existing bootchart JSON depends on this path: those
        # runs were recorded before `seconds` existed.
        legacy = dict(self.boots[0])
        legacy.pop("seconds")
        recovered = bb.run_metrics_seconds(legacy)
        for key, *_ in bb.METRIC_ROWS:
            self.assertEqual(recovered[key], self.boots[0]["seconds"][key], key)

    def test_an_unparseable_metric_is_omitted_not_zeroed(self):
        # A missing boot time and a 0.000 s boot time are very different
        # claims, and averaging the second one silently corrupts a trend.
        legacy = dict(self.boots[0])
        legacy.pop("seconds")
        legacy["metrics"] = dict(legacy["metrics"])
        legacy["metrics"]["kernel"] = {"value": "n/a"}
        recovered = bb.run_metrics_seconds(legacy)
        self.assertNotIn("kernel", recovered)

    def test_adding_seconds_did_not_break_validation(self):
        # _validate_boot checks for required keys and tolerates extras. If
        # that were not true, every run would now fail to record.
        bb._validate_boot(self.boots[0])


# ---------------------------------------------------------------------------
# Non-interactive, cancel, lock
# ---------------------------------------------------------------------------

class TestNonInteractive(unittest.TestCase):
    def tearDown(self):
        bb.NON_INTERACTIVE = False

    def test_a_prompt_becomes_a_usage_error(self):
        # Under the agent, stdin is DEVNULL and input() raises EOFError --
        # a traceback indistinguishable from a real bug. This turns it into
        # a clean, documented exit code.
        bb.NON_INTERACTIVE = True
        with self.assertRaises(bb.BootbenchError) as ctx:
            bb.prompt_yes_no("Proceed? [y/N] ")
        self.assertEqual(ctx.exception.exit_code, bb.EXIT_USAGE)

    def test_interactive_input_is_still_read(self):
        bb.NON_INTERACTIVE = False
        with patch("builtins.input", return_value="y"):
            self.assertTrue(bb.prompt_yes_no("? "))
        with patch("builtins.input", return_value=""):
            self.assertFalse(bb.prompt_yes_no("? "))


class TestCancellation(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir()
        self.flag = self.root / "cancel"

    def tearDown(self):
        bb.CANCEL_FILE = None

    def test_no_cancel_file_configured_is_never_cancelled(self):
        bb.CANCEL_FILE = None
        self.assertFalse(bb.cancel_requested())
        bb.raise_if_cancelled("anywhere")  # must not raise

    def test_an_absent_file_is_not_a_cancel(self):
        bb.CANCEL_FILE = str(self.flag)
        self.assertFalse(bb.cancel_requested())

    def test_a_present_file_cancels_with_exit_130(self):
        bb.CANCEL_FILE = str(self.flag)
        self.flag.write_text("", encoding="utf-8")
        with self.assertRaises(bb.CancelledError) as ctx:
            bb.raise_if_cancelled("before boot debug-2")
        self.assertEqual(ctx.exception.exit_code, bb.EXIT_INTERRUPTED)
        # The message should say where it stopped, since "cancelled" alone
        # doesn't tell you whether the device was left mid-flash.
        self.assertIn("before boot debug-2", str(ctx.exception))


class TestReportLock(unittest.TestCase):
    """Guards the one race the coordinator cannot see: an engineer
    hand-running the skill while a nightly is in flight. Both would read the
    same JSON, append a run, and the second write would drop the first."""

    def setUp(self):
        self.root = tmpdir()
        self.lock = self.root / "sub" / ".bootbench.lock"

    def test_no_lock_file_configured_is_a_no_op(self):
        with bb.report_lock(None) as held:
            self.assertIsNone(held)

    def test_the_lock_is_created_and_released(self):
        with bb.report_lock(self.lock):
            self.assertTrue(self.lock.is_file())
        self.assertFalse(self.lock.exists())

    def test_a_second_holder_is_refused_while_the_first_holds_it(self):
        with bb.report_lock(self.lock):
            with self.assertRaises(RuntimeError) as ctx:
                with bb.report_lock(self.lock):
                    pass
            self.assertIn("lock", str(ctx.exception).lower())

    def test_a_lock_from_a_dead_process_is_broken(self):
        # Otherwise one crashed run wedges every later one -- a worse
        # failure than the race the lock prevents.
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("999999999\n2026-01-01T00:00:00+00:00\n",
                             encoding="utf-8")
        with bb.report_lock(self.lock):
            self.assertTrue(self.lock.is_file())

    def test_the_lock_is_released_even_if_the_body_raises(self):
        with self.assertRaises(ValueError):
            with bb.report_lock(self.lock):
                raise ValueError("boom")
        self.assertFalse(self.lock.exists())


class TestDebugCmdlineDetection(unittest.TestCase):
    """`boot.cmdline_has_debug` is what stops a mislabelled boot from
    silently polluting the trend, so the predicate itself matters."""

    def test_a_default_cmdline_is_not_debug(self):
        self.assertFalse(bb.cmdline_has_debug_params(
            "console=ttyMSM0,115200n8 root=PARTUUID=abc rw"))

    def test_initcall_debug_alone_counts(self):
        self.assertTrue(bb.cmdline_has_debug_params(
            "console=ttyMSM0 rw initcall_debug"))

    def test_log_buf_len_counts(self):
        self.assertTrue(bb.cmdline_has_debug_params("console=ttyMSM0 log_buf_len=4M"))

    def test_systemd_log_level_debug_counts(self):
        self.assertTrue(bb.cmdline_has_debug_params(
            "console=ttyMSM0 systemd.log_level=debug"))

    def test_a_missing_cmdline_is_unknown_not_false(self):
        # The file is read non-strictly, so absent means "we don't know" --
        # claiming False would assert something unverified.
        self.assertIsNone(bb.cmdline_has_debug_params(None))


class TestDocumentedCliSurface(unittest.TestCase):
    """Every invocation in SKILL.md / COMMAND_REFERENCE.md must behave
    exactly as before. The new flags are additive, which means absent is
    the default and the documented commands never mention them."""

    DOCUMENTED = [
        "all --yes",
        "flash",
        "flash --recover",
        "flash capture --yes",
        "flash capture report --yes",
        r"capture --build-path \\host\x\performance",
        r"capture --build-path \\host\x\performance --target t --resume-pull",
        "report --report-cmd render --target iq-9075-evk",
        "report --report-cmd add-run --run-json r.json --target iq-9075-evk",
        "all --yes --num-boots 5",
        "flash --dry-run",
        "flash --skip-edl --yes",
        "flash --tac-port VTP8 --com-port COM7",
        "all --yes --boot-timeout 600",
    ]

    def setUp(self):
        self.parser = bb.build_parser()

    def test_every_documented_invocation_still_parses(self):
        for cmd in self.DOCUMENTED:
            with self.subTest(cmd=cmd):
                self.parser.parse_args(cmd.split())

    def test_every_automation_flag_defaults_to_off(self):
        args = self.parser.parse_args(["all", "--yes"])
        for flag, expected in (
            ("json", False), ("json_status", None), ("adb_serial", None),
            ("boot_charts_dir", None), ("boot_logs_dir", None),
            ("share_root", None), ("non_interactive", False),
            ("lock_file", None), ("cancel_file", None),
            ("revert_debug_cmdline", False),
        ):
            with self.subTest(flag=flag):
                self.assertEqual(getattr(args, flag), expected)

    def test_latest_build_cannot_be_combined_with_other_stages(self):
        # It is a standalone query dispatched before parse_stages(), which
        # would otherwise silently drop it and run the other stages.
        args = self.parser.parse_args(["latest-build", "flash"])
        with self.assertRaises(SystemExit):
            bb._run(args, self.parser)

    def test_stage_order_is_fixed_regardless_of_typing_order(self):
        self.assertEqual(bb.parse_stages(["report", "flash", "capture"]),
                         ["flash", "capture", "report"])
        self.assertEqual(bb.parse_stages(["all"]),
                         ["flash", "capture", "report"])


class TestNumBootsDefault(unittest.TestCase):
    """--num-boots became None-defaulted so --resume-pull can discover what
    is actually on the device. The documented default of 3 has to survive
    that, or every hand-run capture silently changes shape."""

    def setUp(self):
        self.parser = bb.build_parser()

    def seen_num_boots(self, argv):
        args = self.parser.parse_args(argv)
        captured = {}

        def fake_capture(a, handoff=None):
            captured["num_boots"] = a.num_boots

        with patch.object(bb, "cmd_capture", side_effect=fake_capture):
            bb._run(args, self.parser)
        return captured["num_boots"]

    def test_omitting_it_still_means_three(self):
        got = self.seen_num_boots(
            ["capture", "--build-path", r"\\host\x", "--target", "t"])
        self.assertEqual(got, 3)

    def test_an_explicit_value_is_respected(self):
        got = self.seen_num_boots(
            ["capture", "--build-path", r"\\host\x", "--target", "t",
             "--num-boots", "5"])
        self.assertEqual(got, 5)

    def test_a_nonsense_value_falls_back_to_three_as_documented(self):
        got = self.seen_num_boots(
            ["capture", "--build-path", r"\\host\x", "--target", "t",
             "--num-boots", "0"])
        self.assertEqual(got, 3)

    def test_resume_pull_keeps_none_so_it_can_discover_from_the_device(self):
        got = self.seen_num_boots(
            ["capture", "--resume-pull", "--build-path", r"\\host\x",
             "--target", "t"])
        self.assertIsNone(got)


class TestStorageDetection(unittest.TestCase):
    """`lsblk` over the serial console decides PCAT's -MEMORYTYPE.

    Worth automating because the parameter used to be hardcoded to UFS, and
    pointing it at an eMMC board does not fail cleanly -- it tells PCAT to
    address the raw partitions the wrong way.
    """

    def judge(self, text):
        disk, _how = bb._choose_boot_disk(bb._parse_lsblk(text))
        return bb._memory_type_for(disk), disk

    def test_each_storage_class_is_recognised(self):
        cases = [
            ("UFS", "sda", "sda disk\nsda1 part /boot\nsda14 part /"),
            ("eMMC", "mmcblk0", "mmcblk0 disk\nmmcblk0p14 part /"),
            ("NVME", "nvme0n1", "nvme0n1 disk\nnvme0n1p3 part /"),
        ]
        for expected, disk, text in cases:
            with self.subTest(expected):
                self.assertEqual(self.judge(text), (expected, disk))

    def test_an_sd_card_does_not_make_a_ufs_board_look_like_emmc(self):
        """Why detection resolves through `/` rather than a bare prefix.

        A UFS board with a card in the slot reports both `sda` and `mmcblk1`.
        Matching the "mmcblk" prefix would answer eMMC and flash the board the
        wrong way; the partition mounted at `/` cannot be fooled.
        """
        self.assertEqual(
            self.judge("sda disk\nsda14 part /\n"
                       "mmcblk1 disk\nmmcblk1p1 part /media/card"),
            ("UFS", "sda"))

    def test_the_tree_fallback_parses(self):
        """Busybox rejects `-ln -o`, so a bare `lsblk` -- MAJ:MIN column and
        tree glyphs included -- has to parse too."""
        self.assertEqual(
            self.judge("NAME        MAJ:MIN TYPE MOUNTPOINT\n"
                       "mmcblk0   179:0    disk\n"
                       "|-mmcblk0p1 179:1  part /boot\n"
                       "`-mmcblk0p14 179:14 part /"),
            ("eMMC", "mmcblk0"))

    def test_ambiguity_raises_rather_than_guesses(self):
        """Two storage classes, nothing mounted at `/`. A guess here is a coin
        flip on a destructive parameter, so it asks for --memory-type."""
        with self.assertRaises(RuntimeError) as caught:
            self.judge("sda disk\nmmcblk0 disk")
        self.assertIn("--memory-type", str(caught.exception))

    def test_unrecognisable_output_raises(self):
        with self.assertRaises(RuntimeError):
            self.judge("loop0 loop\nram0 disk")

    def test_detect_storage_type_falls_back_to_bare_lsblk(self):
        """The flagged form is tried first; a busybox rejection must not be
        fatal while a parsable bare listing is still available."""
        calls = []

        def fake_cmd(ser, cmd, **kwargs):
            calls.append(cmd)
            if "-o" in cmd:
                raise RuntimeError("lsblk: unrecognized option: l")
            return "mmcblk0 disk\nmmcblk0p14 part /"

        with patch.object(bb, "run_serial_command", fake_cmd):
            got = bb.detect_storage_type(object())
        self.assertEqual(got["memory_type"], "eMMC")
        self.assertEqual(got["disk"], "mmcblk0")
        self.assertEqual(len(calls), 2)

    def test_pcat_argv_carries_the_detected_type(self):
        seen = {}

        class FakeProc:
            stdout = []
            returncode = 0

            def wait(self):
                return 0

        def fake_popen(cmd, **kwargs):
            seen["cmd"] = cmd
            return FakeProc()

        with patch.object(bb.subprocess, "Popen", fake_popen):
            bb.run_pcat_flash("dev1", Path("build"), "eMMC")
        cmd = seen["cmd"]
        self.assertEqual(cmd[cmd.index("-MEMORYTYPE") + 1], "eMMC")

    def test_default_is_unchanged_for_callers_that_omit_it(self):
        """A hand-run `flash` on the original UFS board behaves as before."""
        self.assertEqual(bb.MEMORY_TYPE_DEFAULT, "UFS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
