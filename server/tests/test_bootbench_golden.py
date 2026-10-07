"""Golden-file regression net for `bootbench.py`'s pure output path.

`bootbench.py` is run by hand by engineers today. The automation changes in
Phase 1 are all meant to be *additive* -- new flags, new dict keys, new exit
codes -- with every invocation documented in SKILL.md behaving identically.
This module is the proof of that claim rather than the hope of it: it parses
a fixture log tree through the real `read_pulled_logs` / `build_run` and
asserts the rendered HTML, the `.txt` backup, and the full run dicts are
byte-identical to snapshots taken from the unmodified script.

If a Phase 1 edit changes any of these, the test fails and the diff says
where. If the change was intended, regenerate deliberately:

    py -3 tests/fixtures/make_golden.py

and the regenerated golden shows up in review as its own diff, which is
exactly the visibility an output-format change should have.

The fixtures exercise all six METRIC_ROWS, hitters in three of them, kernel
initcall hitters in the debug phase only, and the overall_overheads pooling
across all three hitter sources -- so this is not a smoke test of an empty
report.
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fixtures"))

import make_golden  # noqa: E402

GOLDEN = make_golden.GOLDEN


def read_golden(name: str) -> str:
    # newline="" on both sides: these snapshots are compared as exact text,
    # and letting the platform translate newlines on read would make the
    # test pass on one OS and fail on another for no real reason.
    return (GOLDEN / name).read_text(encoding="utf-8", newline="")


class TestGoldenOutput(unittest.TestCase):
    """The three outputs a human actually sees must not move."""

    @classmethod
    def setUpClass(cls):
        make_golden.write_fixtures()
        cls.produced = make_golden.make_golden()

    def assertGolden(self, name):
        path = GOLDEN / name
        if not path.is_file():
            self.skipTest(f"no golden snapshot yet: run make_golden.py ({name})")
        expected = read_golden(name)
        actual = self.produced[name]
        if expected != actual:
            self.fail(
                f"{name} is no longer byte-identical to its golden snapshot.\n"
                f"{make_golden._describe_diff(expected, actual)}\n"
                f"If this change was intended, regenerate with "
                f"`py -3 tests/fixtures/make_golden.py`."
            )

    def test_report_html_is_byte_identical(self):
        self.assertGolden("report.html")

    def test_run_txt_backup_is_byte_identical(self):
        self.assertGolden("run_txt.txt")

    def test_parsed_run_dicts_are_byte_identical(self):
        self.assertGolden("boots.json")


class TestFixturesAreDiscriminating(unittest.TestCase):
    """A golden test over a degenerate fixture proves nothing. These assert
    the fixture tree actually reaches every branch worth protecting."""

    @classmethod
    def setUpClass(cls):
        make_golden.write_fixtures()
        cls.boots = make_golden.build_boots()

    def test_both_phases_and_all_boots_are_present(self):
        self.assertEqual(len(self.boots), 6)
        self.assertEqual(
            [b["log_name"] for b in self.boots],
            ["default-1", "default-2", "default-3",
             "debug-1", "debug-2", "debug-3"],
        )

    def test_every_metric_row_parsed_a_value(self):
        for boot in self.boots:
            for key, row in boot["metrics"].items():
                self.assertTrue(
                    row.get("value"), f"{boot['log_name']}:{key} has no value"
                )

    def test_hitters_are_populated_where_the_skill_computes_them(self):
        default = self.boots[0]["metrics"]
        self.assertEqual(len(default["sysinit_svc"]["hitters"]), 5)
        self.assertEqual(len(default["total_multiuser"]["hitters"]), 6)

    def test_kernel_hitters_appear_only_in_the_debug_phase(self):
        # initcall_debug is appended to the cmdline for the debug boots only,
        # so a default boot physically cannot have initcall timings. A fixture
        # where both phases looked alike would hide a regression in the
        # phase-dependent branch of build_run.
        for boot in self.boots:
            hitters = boot["metrics"]["kernel"].get("hitters")
            if boot["phase"] == "debug":
                self.assertTrue(hitters, boot["log_name"])
            else:
                self.assertFalse(hitters, boot["log_name"])

    def test_the_two_phases_produce_different_numbers(self):
        # Same systemd-analyze fixture, different dmesg timings -- so the
        # dmesg-derived metrics must differ while the SAT-derived ones match.
        default, debug = self.boots[0]["metrics"], self.boots[3]["metrics"]
        self.assertNotEqual(default["initramfs"]["value"],
                            debug["initramfs"]["value"])
        self.assertEqual(default["nhlos"]["value"], debug["nhlos"]["value"])

    def test_overall_overheads_pools_and_ranks(self):
        overheads = self.boots[0]["overall_overheads"]
        self.assertEqual(len(overheads), 3)
        seconds = [float(o["time"].split()[0]) for o in overheads]
        self.assertEqual(seconds, sorted(seconds, reverse=True))

    def test_critical_chain_is_parsed_in_order(self):
        self.assertEqual(
            self.boots[0]["critical_chain"][0], "multi-user.target"
        )
        self.assertEqual(
            self.boots[0]["critical_chain"][-1], "sysinit.target"
        )

    def test_optimization_possibilities_is_left_for_a_human(self):
        # The one judgment step in the skill. Automation must never fill it.
        for boot in self.boots:
            self.assertIsNone(boot["optimization_possibilities"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
