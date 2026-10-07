"""`decide()`: every branch, in order, including the ones that do nothing.

The decision table is the heart of the nightly, and most of its outcomes are
*skips*. That is what makes it worth testing exhaustively: a bug here does
not raise, it just quietly stops benchmarking and the dashboard keeps
showing last week's numbers as if they were current.

Four properties beyond branch coverage:

  * **cheapest first.** Steps 1-5 must not touch the network. The stub
    runner here counts calls and the tests assert zero for every outcome
    that should have been answerable from the database alone -- a tick fires
    96 times a day and must cost nothing when there is nothing to do.
  * **the retry check runs before the due check.** A retry is scheduled 30
    minutes after a transient failure, not at the next cron window.
  * **catch-up, not wall-clock matching.** A window missed because the
    coordinator was down is still caught later the same day.
  * **a retry is still health-gated.** Otherwise a logged-out bench host
    collects a retry storm.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.inventory import Device  # noqa: E402
from src.runner.agent_client import AgentUnreachable  # noqa: E402
from src.scheduler import nightly  # noqa: E402

NOW = datetime(2026, 10, 6, 1, 4, tzinfo=timezone.utc)

# A last_tick_at from which the default `0 1 * * *` window has *already*
# closed: (01:02, 01:04] contains no fire time. Using something like
# 00:49 here would silently make every "not due" test due.
NOT_DUE = NOW - timedelta(minutes=2)

HEALTHY = {
    "agent_version": "1.0.0", "hostname": "bench-win-01",
    "session_id": 1, "session_interactive": True,
    "pcat_present": True, "tac_device_count": 1,
    "artifact_root_free_gb": 412.5,
}

BUILD = {"build_number": 2471, "build_folder": "..._Nightly_Build_master_2471",
         "build_path": r"\\share\Yocto\...\performance",
         "target_image_ready": True}


def device(**kw):
    base = dict(device_id="dev-01", target="iq-9075-evk",
                agent_url="http://bench-win-01:8765", com_port="COM7",
                tac_port="VTP8", adb_serial="1a2b3c")
    base.update(kw)
    return Device(**base)


class Settings:
    retry_delay_minutes = 30


class FakeQueries:
    """Stands in for `src.db.queries`, counting nothing but answering
    exactly the five calls `decide` can make."""

    def __init__(self, *, active=None, last=None, state=None, depth=0,
                 benchmarked=False):
        self._active = active
        self._last = last
        self._state = state
        self._depth = depth
        self._benchmarked = benchmarked
        self.enqueued = []
        self.decisions = []
        self.touched = []

    def active_run(self, conn, device_id):
        return self._active

    def last_run(self, conn, device_id):
        return self._last

    def get_scheduler_state(self, conn, device_id):
        return self._state

    def retry_depth(self, conn, run_id):
        return self._depth

    def is_build_benchmarked(self, conn, device_id, build_number):
        return self._benchmarked

    # -- apply() ---------------------------------------------------------
    def enqueue_run(self, conn, dev, **kw):
        self.enqueued.append({"device_id": dev.device_id, **kw})
        return 500 + len(self.enqueued)

    def record_decision(self, conn, device_id, action, reason, **kw):
        self.decisions.append((device_id, action, reason, kw))

    def touch_scheduler_state(self, conn, device_id, **kw):
        self.touched.append((device_id, kw))


class StubRunner:
    """Counts network calls so "cheapest first" can be asserted."""

    def __init__(self, health=None, latest=None, health_error=None,
                 latest_error=None):
        self._health = health if health is not None else dict(HEALTHY)
        self._latest = latest if latest is not None else {"build": dict(BUILD)}
        self._health_error = health_error
        self._latest_error = latest_error
        self.calls = []

    def healthz(self):
        self.calls.append("healthz")
        if self._health_error:
            raise AgentUnreachable(self._health_error)
        return self._health

    def latest_build(self, device_id):
        self.calls.append("latest_build")
        if self._latest_error:
            raise AgentUnreachable(self._latest_error)
        return self._latest

    def recover(self, device_id):
        self.calls.append("recover")
        return {"ok": True}


def state(last_tick):
    return {"last_tick_at": last_tick, "last_action": None,
            "last_reason": None, "consecutive_errors": 0}


def run(status="failed", **kw):
    base = {"run_id": 42, "status": status, "failure_stage": None,
            "exit_code": None, "acknowledged_at": None,
            "finished_at": NOW - timedelta(hours=2)}
    base.update(kw)
    return base


class DecideCase(unittest.TestCase):
    """Common harness: patches `queries` and returns the decision."""

    def decide(self, dev=None, *, runner=None, force=False, now=NOW,
               **query_kw):
        self.q = FakeQueries(**query_kw)
        self.runner = runner if runner is not None else StubRunner()
        with patch.object(nightly, "queries", self.q):
            return nightly.decide(object(), dev or device(),
                                  settings=Settings(), now=now,
                                  runner=self.runner, force=force)

    def assertNoNetwork(self):
        self.assertEqual(self.runner.calls, [],
                         "this outcome must be answerable from the database")


# ---------------------------------------------------------------------------
# the ten branches, in order
# ---------------------------------------------------------------------------

class TestDecideBranches(DecideCase):
    def test_1_disabled(self):
        decision = self.decide(device(enabled=False))
        self.assertEqual((decision.action, decision.reason),
                         ("skipped", "disabled"))
        self.assertNoNetwork()

    def test_2_overlap(self):
        decision = self.decide(active={"run_id": 7, "status": "running"})
        self.assertEqual(decision.reason, "overlap")
        self.assertEqual(decision.detail["run_id"], 7)
        self.assertNoNetwork()

    def test_3_unreachable_blocks_until_acknowledged(self):
        decision = self.decide(last=run("unreachable"),
                               state=state(NOW - timedelta(days=1)))
        self.assertEqual(decision.action, "blocked")
        self.assertEqual(decision.reason, "unreachable_unacknowledged")
        self.assertNoNetwork()

    def test_3_acknowledged_unreachable_no_longer_blocks(self):
        decision = self.decide(
            last=run("unreachable", acknowledged_at=NOW - timedelta(hours=1)),
            state=state(NOW - timedelta(minutes=20)))
        self.assertNotEqual(decision.reason, "unreachable_unacknowledged")

    def test_5_first_tick_seeds_the_clock(self):
        """Installing the crontab at 14:00 must not flash a board at 14:00."""
        decision = self.decide(state=None)
        self.assertEqual(decision.reason, "first_tick")
        self.assertNoNetwork()

    def test_5_not_due(self):
        decision = self.decide(state=state(NOT_DUE))
        self.assertEqual(decision.reason, "not_due")
        self.assertEqual(decision.detail["schedule"], "0 1 * * *")
        self.assertNoNetwork()

    def test_6_agent_unhealthy_on_session_zero(self):
        """The whole reason the health gate exists: a TAC in session 0 is a
        clean skip at 01:00, not a failed flash at 01:40."""
        health = dict(HEALTHY, session_id=0, session_interactive=False)
        decision = self.decide(state=state(NOW - timedelta(hours=2)),
                               runner=StubRunner(health=health))
        self.assertEqual((decision.action, decision.reason),
                         ("error", "agent_unhealthy"))
        self.assertTrue(any("session 0" in r
                            for r in decision.detail["reasons"]))
        self.assertEqual(self.runner.calls, ["healthz"])

    def test_6_agent_unhealthy_on_no_tac(self):
        health = dict(HEALTHY, tac_device_count=0)
        decision = self.decide(state=state(NOW - timedelta(hours=2)),
                               runner=StubRunner(health=health))
        self.assertEqual(decision.reason, "agent_unhealthy")

    def test_6_agent_unreachable_is_an_error_not_a_block(self):
        decision = self.decide(state=state(NOW - timedelta(hours=2)),
                               runner=StubRunner(health_error="connect timed out"))
        self.assertEqual((decision.action, decision.reason),
                         ("error", "agent_unhealthy"))
        self.assertIn("connect timed out", decision.detail["error"])

    def test_7_discovery_failed(self):
        decision = self.decide(
            state=state(NOW - timedelta(hours=2)),
            runner=StubRunner(latest_error="powershell exited 1"))
        self.assertEqual((decision.action, decision.reason),
                         ("error", "discovery_failed"))

    def test_8_image_not_ready_is_a_skip_not_a_failure(self):
        """The commonest overnight outcome: the nightly published but this
        target's image has not landed. Must not look like a failure."""
        latest = {"build": dict(BUILD, target_image_ready=False,
                                build_dir=r"\\share\...\iq-9075-evk")}
        decision = self.decide(state=state(NOW - timedelta(hours=2)),
                               runner=StubRunner(latest=latest))
        self.assertEqual((decision.action, decision.reason),
                         ("skipped", "image_not_ready"))
        self.assertEqual(decision.build["build_number"], 2471)

    def test_9_unparseable_build(self):
        latest = {"build": dict(BUILD, build_number=None,
                                build_folder="release_candidate_7")}
        decision = self.decide(state=state(NOW - timedelta(hours=2)),
                               runner=StubRunner(latest=latest))
        self.assertEqual(decision.reason, "unparseable_build")
        self.assertEqual(decision.detail["build_folder"],
                         "release_candidate_7")

    def test_10_already_benchmarked(self):
        decision = self.decide(state=state(NOW - timedelta(hours=2)),
                               benchmarked=True)
        self.assertEqual((decision.action, decision.reason),
                         ("skipped", "already_benchmarked"))

    def test_10_new_build_enqueues(self):
        decision = self.decide(state=state(NOW - timedelta(hours=2)))
        self.assertEqual((decision.action, decision.reason),
                         ("enqueued", "new_build"))
        self.assertTrue(decision.enqueues)
        self.assertEqual(decision.build["build_number"], 2471)
        self.assertEqual(decision.stages, "all")
        self.assertEqual(decision.trigger_source, "cron")
        self.assertEqual(self.runner.calls, ["healthz", "latest_build"])

    def test_every_reason_is_declared(self):
        """A reason string with no REASONS entry renders as a mystery row on
        /schedule and has no action, so `describe()` would lie."""
        for reason in ("disabled", "overlap", "unreachable_unacknowledged",
                       "first_tick", "not_due", "agent_unhealthy",
                       "discovery_failed", "image_not_ready",
                       "unparseable_build", "already_benchmarked",
                       "new_build", "forced", "retry_pending",
                       "retries_exhausted", "timeout", "retry_tac",
                       "retry_edl", "retry_serial_login",
                       "retry_boot_collect", "retry_adb"):
            with self.subTest(reason=reason):
                self.assertIn(reason, nightly.REASONS)


# ---------------------------------------------------------------------------
# force
# ---------------------------------------------------------------------------

class TestForce(DecideCase):
    def test_force_overrides_not_due_and_already_benchmarked(self):
        decision = self.decide(state=state(NOW - timedelta(minutes=5)),
                               benchmarked=True, force=True)
        self.assertEqual((decision.action, decision.reason),
                         ("enqueued", "forced"))

    def test_force_works_with_no_scheduler_state(self):
        decision = self.decide(state=None, force=True)
        self.assertEqual(decision.reason, "forced")

    def test_force_does_not_override_overlap(self):
        """Two concurrent runs on one board is damage, not a preference."""
        decision = self.decide(active={"run_id": 7, "status": "running"},
                               force=True)
        self.assertEqual(decision.reason, "overlap")

    def test_force_does_not_override_disabled_or_blocked(self):
        decision = self.decide(device(enabled=False), force=True)
        self.assertEqual(decision.reason, "disabled")
        decision = self.decide(last=run("unreachable"), force=True)
        self.assertEqual(decision.reason, "unreachable_unacknowledged")

    def test_force_does_not_resume_a_pull(self):
        """Someone pressing "Run now" after an adb failure wants a full run,
        not the resumed pull the retry policy would have chosen."""
        decision = self.decide(last=run("failed", failure_stage="adb"),
                               state=state(NOW - timedelta(days=1)),
                               force=True)
        self.assertEqual(decision.reason, "forced")
        self.assertEqual(decision.stages, "all")

    def test_force_still_respects_the_health_gate(self):
        health = dict(HEALTHY, tac_device_count=0)
        decision = self.decide(state=None, force=True,
                               runner=StubRunner(health=health))
        self.assertEqual(decision.reason, "agent_unhealthy")


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------

class TestRetryOrdering(DecideCase):
    def test_retry_is_evaluated_before_the_due_check(self):
        """The property the module docstring calls out: a retry 30 minutes
        after a 01:00 failure must fire at 01:30, not at 01:00 tomorrow."""
        decision = self.decide(
            last=run("failed", failure_stage="tac",
                     finished_at=NOW - timedelta(minutes=45)),
            state=state(NOT_DUE))  # not due
        self.assertEqual((decision.action, decision.reason),
                         ("enqueued", "retry_tac"))
        self.assertEqual(decision.trigger_source, "retry")
        self.assertEqual(decision.parent_run_id, 42)
        self.assertTrue(decision.recover_first)

    def test_retry_waits_out_the_delay(self):
        decision = self.decide(
            last=run("failed", failure_stage="tac",
                     finished_at=NOW - timedelta(minutes=5)),
            state=state(NOT_DUE))
        self.assertEqual((decision.action, decision.reason),
                         ("skipped", "retry_pending"))
        self.assertIn("due_at", decision.detail)

    def test_adb_retry_resumes_the_pull(self):
        decision = self.decide(
            last=run("failed", failure_stage="adb"),
            state=state(NOT_DUE))
        self.assertEqual(decision.reason, "retry_adb")
        self.assertEqual(decision.stages, "capture")
        self.assertFalse(decision.recover_first)

    def test_retries_exhausted(self):
        decision = self.decide(
            last=run("failed", failure_stage="tac"),
            state=state(NOT_DUE), depth=1)
        self.assertEqual((decision.action, decision.reason),
                         ("skipped", "retries_exhausted"))
        self.assertEqual(decision.detail["attempts"], 1)
        self.assertNoNetwork()

    def test_max_retries_zero_disables_retries(self):
        decision = self.decide(
            device(max_retries=0), last=run("failed", failure_stage="tac"),
            state=state(NOT_DUE))
        self.assertEqual(decision.reason, "not_due")

    def test_non_retryable_failure_falls_through_to_the_due_check(self):
        decision = self.decide(last=run("failed", failure_stage="flash"),
                               state=state(NOT_DUE))
        self.assertEqual(decision.reason, "not_due")

    def test_timeout_is_retried(self):
        decision = self.decide(last=run("timeout"),
                               state=state(NOT_DUE))
        self.assertEqual(decision.reason, "timeout")
        self.assertTrue(decision.enqueues)

    def test_partial_is_not_retried(self):
        decision = self.decide(last=run("partial"),
                               state=state(NOT_DUE))
        self.assertEqual(decision.reason, "not_due")

    def test_retry_is_health_gated(self):
        """Otherwise a logged-out bench host collects a retry every fifteen
        minutes until someone notices."""
        health = dict(HEALTHY, session_interactive=False, session_id=0)
        decision = self.decide(
            last=run("failed", failure_stage="tac"),
            state=state(NOT_DUE),
            runner=StubRunner(health=health))
        self.assertEqual(decision.reason, "agent_unhealthy")
        self.assertEqual(decision.detail["for"], "retry")

    def test_pending_retry_is_not_reported_as_unhealthy(self):
        """A decision that was going to wait anyway must not be relabelled;
        the decision log would be lying about why nothing ran."""
        health = dict(HEALTHY, tac_device_count=0)
        decision = self.decide(
            last=run("failed", failure_stage="tac",
                     finished_at=NOW - timedelta(minutes=5)),
            state=state(NOT_DUE),
            runner=StubRunner(health=health))
        self.assertEqual(decision.reason, "retry_pending")
        self.assertNoNetwork()

    def test_retry_skips_build_discovery(self):
        """A retry re-runs the build that failed; asking for the newest one
        would quietly change what is being measured."""
        decision = self.decide(
            last=run("failed", failure_stage="edl"),
            state=state(NOT_DUE), benchmarked=True)
        self.assertTrue(decision.enqueues)
        self.assertEqual(self.runner.calls, ["healthz"])


# ---------------------------------------------------------------------------
# cron catch-up
# ---------------------------------------------------------------------------

class TestWasDue(unittest.TestCase):
    def test_window_crossed(self):
        self.assertTrue(nightly.was_due(
            "0 1 * * *",
            datetime(2026, 10, 6, 0, 50, tzinfo=timezone.utc),
            datetime(2026, 10, 6, 1, 5, tzinfo=timezone.utc)))

    def test_window_not_crossed(self):
        self.assertFalse(nightly.was_due(
            "0 1 * * *",
            datetime(2026, 10, 6, 1, 5, tzinfo=timezone.utc),
            datetime(2026, 10, 6, 1, 20, tzinfo=timezone.utc)))

    def test_missed_window_is_caught_later(self):
        """The coordinator was down from 00:30 to 09:00. The 01:00 window is
        still honoured when it comes back, rather than lost until tomorrow."""
        self.assertTrue(nightly.was_due(
            "0 1 * * *",
            datetime(2026, 10, 6, 0, 30, tzinfo=timezone.utc),
            datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)))

    def test_left_edge_is_open_so_a_window_fires_once(self):
        """A tick landing exactly on 01:00 enqueues; the next tick must not
        enqueue again for the same window."""
        fire = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)
        self.assertFalse(nightly.was_due("0 1 * * *", fire,
                                         fire + timedelta(minutes=15)))

    def test_no_last_tick_is_never_due(self):
        self.assertFalse(nightly.was_due("0 1 * * *", None, NOW))

    def test_clock_moving_backwards_is_not_due(self):
        self.assertFalse(nightly.was_due(
            "0 1 * * *", NOW, NOW - timedelta(hours=1)))

    def test_naive_timestamps_are_treated_as_utc(self):
        self.assertTrue(nightly.was_due(
            "0 1 * * *", datetime(2026, 10, 6, 0, 50),
            datetime(2026, 10, 6, 1, 5)))

    def test_schedule_validation(self):
        self.assertTrue(nightly.schedule_is_valid("0 1 * * *"))
        self.assertTrue(nightly.schedule_is_valid("*/15 * * * *"))
        self.assertFalse(nightly.schedule_is_valid("not a cron"))
        self.assertFalse(nightly.schedule_is_valid("99 1 * * *"))


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

class TestApply(unittest.TestCase):
    def setUp(self):
        self.q = FakeQueries()
        self.device = device()

    def apply(self, decision, *, dry_run=False, runner=None):
        with patch.object(nightly, "queries", self.q):
            return nightly.apply(object(), self.device, decision,
                                 runner=runner, dry_run=dry_run)

    def test_enqueue_records_both_rows(self):
        decision = nightly._decision("dev-01", "new_build", build=dict(BUILD),
                                     stages="all")
        applied = self.apply(decision)
        self.assertEqual(applied.run_id, 501)
        self.assertEqual(len(self.q.enqueued), 1)
        self.assertEqual(self.q.enqueued[0]["triggered_by"], "scheduler")
        self.assertEqual(len(self.q.decisions), 1)
        self.assertEqual(self.q.decisions[0][2], "new_build")
        self.assertEqual(self.q.decisions[0][3]["build_number"], 2471)
        self.assertEqual(self.q.decisions[0][3]["run_id"], 501)

    def test_skip_records_the_decision_but_no_run(self):
        applied = self.apply(nightly._decision("dev-01", "not_due"))
        self.assertIsNone(applied.run_id)
        self.assertEqual(self.q.enqueued, [])
        self.assertEqual(len(self.q.decisions), 1)

    def test_error_marks_the_scheduler_state(self):
        self.apply(nightly._decision("dev-01", "agent_unhealthy"))
        self.assertTrue(self.q.touched[0][1]["errored"])

    def test_dry_run_writes_nothing(self):
        applied = self.apply(nightly._decision("dev-01", "new_build",
                                               build=dict(BUILD)),
                             dry_run=True)
        self.assertIsNone(applied.run_id)
        self.assertEqual(self.q.enqueued, [])
        self.assertEqual(self.q.decisions, [])
        self.assertEqual(self.q.touched, [])

    def test_recover_runs_before_an_enqueue_that_asked_for_it(self):
        runner = StubRunner()
        decision = nightly.Decision(device_id="dev-01", action="enqueued",
                                    reason="timeout", recover_first=True)
        self.apply(decision, runner=runner)
        self.assertEqual(runner.calls, ["recover"])

    def test_failed_recovery_does_not_block_the_retry(self):
        """The run itself will fail at the EDL stage and say so, which is a
        better signal than refusing to retry at all."""
        class Broken(StubRunner):
            def recover(self, device_id):
                raise AgentUnreachable("agent is gone")

        decision = nightly.Decision(device_id="dev-01", action="enqueued",
                                    reason="timeout", recover_first=True)
        applied = self.apply(decision, runner=Broken())
        self.assertEqual(applied.run_id, 501)

    def test_recover_is_skipped_when_not_asked_for(self):
        runner = StubRunner()
        decision = nightly.Decision(device_id="dev-01", action="enqueued",
                                    reason="retry_adb", stages="capture",
                                    recover_first=False)
        self.apply(decision, runner=runner)
        self.assertEqual(runner.calls, [])


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------

class TestTick(unittest.TestCase):
    def test_health_is_asked_once_per_agent_not_per_device(self):
        """Two boards on one bench host share both answers; four identical
        calls every fifteen minutes is waste the tick cannot afford."""
        devices = [device(device_id="dev-01"),
                   device(device_id="dev-02", adb_serial="9z8y7x")]
        runner = StubRunner()
        q = FakeQueries(state=state(NOW - timedelta(hours=2)))

        with patch.object(nightly, "queries", q):
            decisions = nightly.tick(object(), devices, settings=Settings(),
                                     runner_for=lambda d: runner, now=NOW)

        self.assertEqual(len(decisions), 2)
        self.assertEqual(runner.calls.count("healthz"), 1)

    def test_only_filters_devices(self):
        devices = [device(device_id="dev-01"), device(device_id="dev-02")]
        q = FakeQueries(state=state(NOW - timedelta(hours=2)))
        with patch.object(nightly, "queries", q):
            decisions = nightly.tick(object(), devices, settings=Settings(),
                                     runner_for=lambda d: StubRunner(),
                                     now=NOW, only={"dev-02"})
        self.assertEqual([d.device_id for d in decisions], ["dev-02"])

    def test_disabled_device_is_never_asked_about(self):
        runner = StubRunner()
        q = FakeQueries(state=state(NOW - timedelta(hours=2)))
        with patch.object(nightly, "queries", q):
            nightly.tick(object(), [device(enabled=False)],
                         settings=Settings(), runner_for=lambda d: runner,
                         now=NOW)
        self.assertEqual(runner.calls, [])

    def test_dry_run_tick_writes_nothing(self):
        q = FakeQueries(state=state(NOW - timedelta(hours=2)))
        with patch.object(nightly, "queries", q):
            decisions = nightly.tick(object(), [device()], settings=Settings(),
                                     runner_for=lambda d: StubRunner(),
                                     now=NOW, dry_run=True)
        self.assertEqual(decisions[0].reason, "new_build")
        self.assertEqual(q.enqueued, [])
        self.assertEqual(q.decisions, [])

    def test_describe_names_the_action_and_a_sentence(self):
        text = nightly._decision("dev-01", "image_not_ready").describe()
        self.assertTrue(text.startswith("skipped/image_not_ready:"))
        self.assertIn("image has not landed", text)


if __name__ == "__main__":
    unittest.main()
