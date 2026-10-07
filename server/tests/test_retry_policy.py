"""Which failures earn another forty minutes of bench time.

The policy is a table, so these tests are mostly a table too. Two of them
earn their keep beyond that:

  * the `adb` retry must come back as `capture` and must *not* recover
    first -- power-cycling the board into EDL would delete the very logs
    the retry exists to collect;
  * `_stage_from_exit_code` is a copy of the skill's `EXIT_NAMES`, and a
    copy that drifts would silently retry the wrong failures. The test
    compares it against the real table.
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.scheduler import retry  # noqa: E402


class TestPlanFor(unittest.TestCase):
    def test_transient_stages_retry_a_full_run(self):
        for stage in ("tac", "edl", "serial_login", "boot_collect"):
            with self.subTest(stage=stage):
                plan = retry.plan_for("failed", failure_stage=stage)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.reason, f"retry_{stage}")
                self.assertEqual(plan.stages, "all")
                self.assertTrue(plan.recover_first)
                self.assertFalse(plan.resume_pull)

    def test_deterministic_stages_are_left_alone(self):
        for stage in ("flash", "parse", "record", "usage", "build_discovery",
                      "unknown", "interrupted"):
            with self.subTest(stage=stage):
                self.assertIsNone(retry.plan_for("failed",
                                                 failure_stage=stage))

    def test_flash_is_never_retried(self):
        """Separately from the table above, because this is the one whose
        retry could do physical damage: the partition may be mid-write."""
        self.assertIsNone(retry.plan_for("failed", failure_stage="flash"))
        self.assertIsNone(retry.plan_for("failed", exit_code=12))

    def test_adb_resumes_the_pull_without_recovering(self):
        plan = retry.plan_for("failed", failure_stage="adb")
        self.assertEqual(plan.stages, "capture")
        self.assertTrue(plan.resume_pull)
        # The device is booted and reachable over adb. Recovery would put it
        # back in EDL and lose the logs.
        self.assertFalse(plan.recover_first)

    def test_timeout_retries_with_recovery(self):
        plan = retry.plan_for("timeout")
        self.assertEqual(plan.reason, "timeout")
        self.assertEqual(plan.stages, "all")
        self.assertTrue(plan.recover_first)

    def test_statuses_that_never_retry(self):
        for status in ("success", "partial", "cancelled", "unreachable"):
            with self.subTest(status=status):
                self.assertIsNone(retry.plan_for(status))

    def test_partial_is_not_retried_even_with_a_retryable_stage(self):
        """A partial run has numbers in the database. Re-running it trades
        them for a chance at more, which is the wrong direction."""
        self.assertIsNone(retry.plan_for("partial", failure_stage="tac"))

    def test_unknown_status_is_not_retried(self):
        self.assertIsNone(retry.plan_for("queued"))
        self.assertIsNone(retry.plan_for(None))
        self.assertIsNone(retry.plan_for(""))

    def test_stage_wins_over_exit_code(self):
        """The stage is the more specific signal; the exit code is only for
        a run that died without writing a status document."""
        plan = retry.plan_for("failed", failure_stage="flash", exit_code=18)
        self.assertIsNone(plan)
        plan = retry.plan_for("failed", failure_stage="tac", exit_code=12)
        self.assertEqual(plan.reason, "retry_tac")

    def test_exit_code_is_used_when_no_stage_was_recorded(self):
        plan = retry.plan_for("failed", exit_code=18)
        self.assertEqual(plan.reason, "retry_tac")
        plan = retry.plan_for("failed", exit_code=15)
        self.assertEqual(plan.stages, "capture")

    def test_failed_with_nothing_recorded_is_not_retried(self):
        """No stage and no exit code means nobody knows what broke. Another
        hour of bench time on a mystery is not a good trade."""
        self.assertIsNone(retry.plan_for("failed"))

    def test_garbage_exit_code_is_tolerated(self):
        for value in ("", "abc", None, 9999, 1.5):
            with self.subTest(value=value):
                self.assertIsNone(retry.plan_for("failed", exit_code=value))

    def test_case_and_whitespace_are_normalised(self):
        plan = retry.plan_for("  FAILED ", failure_stage=" TAC ")
        self.assertEqual(plan.reason, "retry_tac")

    def test_describe_is_readable(self):
        self.assertEqual(retry.describe(None), "no retry")
        text = retry.describe(retry.plan_for("failed", failure_stage="adb"))
        self.assertIn("retry_adb", text)
        self.assertIn("stages=capture", text)


class TestTableHygiene(unittest.TestCase):
    def test_no_stage_is_in_two_tables(self):
        """A stage in both RETRY_STAGES and NO_RETRY_STAGES would make the
        policy depend on the order of the `if`s rather than on the table."""
        tables = (retry.RETRY_STAGES, retry.RESUME_STAGES,
                  retry.NO_RETRY_STAGES)
        seen = set()
        for table in tables:
            overlap = seen & set(table)
            self.assertEqual(overlap, set(), f"stage in two tables: {overlap}")
            seen |= set(table)

    def test_no_status_is_in_two_tables(self):
        self.assertEqual(set(retry.RETRY_STATUSES)
                         & set(retry.NO_RETRY_STATUSES), set())

    def test_every_exit_name_has_a_policy(self):
        """A stage with no entry anywhere falls through to "no retry" by
        accident rather than by decision. `ok` is excluded: it is not a
        failure stage."""
        covered = (set(retry.RETRY_STAGES) | set(retry.RESUME_STAGES)
                   | set(retry.NO_RETRY_STAGES))
        names = {retry._stage_from_exit_code(code)
                 for code in (1, 2, 10, 11, 12, 13, 14, 15, 16, 17, 18, 130)}
        self.assertEqual(names - covered, set())

    def test_exit_table_matches_the_skill(self):
        """`_stage_from_exit_code` is a deliberate copy of the skill's
        EXIT_NAMES so the policy stays evaluable when `bootbench_path` does
        not resolve. A copy that drifts retries the wrong failures."""
        sys.path.insert(0, str(HERE / "fixtures"))
        try:
            import make_golden  # noqa: F401 - puts the skill on sys.path
            import bootbench as bb
        except Exception as exc:  # pragma: no cover - no skill checked out
            self.skipTest(f"skill not importable: {exc}")

        for code, name in bb.EXIT_NAMES.items():
            with self.subTest(code=code):
                self.assertEqual(retry._stage_from_exit_code(code), name)
        # And nothing extra: an exit code the skill does not define must not
        # resolve to a stage name here.
        self.assertEqual(retry._stage_from_exit_code(77), "")


if __name__ == "__main__":
    unittest.main()
