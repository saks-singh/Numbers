"""Which failures are worth another hour of bench time.

A retry is not free: a full `all` run is roughly forty minutes of flashing
and booting, during which the board cannot do anything else. So the default
is *not* to retry, and a failure has to make a specific case for itself.

The division is between failures that are plausibly transient -- a TAC that
did not enumerate, a serial login that missed its prompt, an EDL device that
took too long to appear -- and failures that are deterministic. Re-running a
parse error reproduces the parse error, an hour later, having told you
nothing new. Re-running a flash failure is worse than useless: the boot
partition may be mid-write, and the right next step is a human looking at
it, not another PCAT invocation.

The retry is *enqueued by the scheduler*, not by the worker that saw the
failure. The worker would have to sleep through `retry_delay_minutes` to
honour the delay, holding a device lock while doing nothing; the scheduler
is already waking every fifteen minutes and already owns every decision to
create a run. One place creates runs, and a worker restart cannot lose a
pending retry.
"""

from __future__ import annotations

from dataclasses import dataclass

# bootbench's `failure_stage`, which is also EXIT_NAMES[exit_code]. Matching
# on the name rather than the number because the name is what the status
# document and the dashboard both show, so a policy decision can be read off
# a run page without a lookup table.
RETRY_STAGES = {
    "tac": "tac did not enumerate; usually a USB/COM transient",
    "edl": "device never appeared in EDL",
    "serial_login": "console login missed its prompt",
    "boot_collect": "a boot did not complete in time",
}

# Retried, but as a *different* job: the logs are already on the device, so
# re-flashing and booting six more times throws away the forty minutes that
# already succeeded. `capture --resume-pull` picks up at the pull.
RESUME_STAGES = {
    "adb": "logs are on the device; resume at the pull rather than reflash",
}

# Spelled out rather than left as "everything else" so each decision is a
# statement someone can disagree with in review.
NO_RETRY_STAGES = {
    "flash": "a partition may be mid-write; needs human eyes before another "
             "flash",
    "parse": "deterministic; the same logs parse the same way",
    "record": "deterministic; a retry wastes an hour",
    "usage": "a config or argv bug; a retry cannot fix it",
    "build_discovery": "the next tick retries this for free",
    "unknown": "an unhandled exception; read the traceback first",
    "interrupted": "someone stopped it on purpose",
}

# Run-level outcomes that have no bootbench stage at all.
RETRY_STATUSES = {
    "timeout": "the job exceeded its budget; the board may just be wedged",
}

# `error_class` as the worker records it, consulted only when there is no
# stage and no exit code to go on. A bench host that logs off or reboots
# mid-job kills bootbench.py hard enough that its `finally` never runs, so
# the run arrives here with no stage, no exit code, and nothing to match --
# and without this it would be dropped silently, which costs a whole night
# for a cause that is gone by the time anyone looks.
RETRY_ERROR_CLASSES = {
    "NoStatusDocument": "the job died without finalizing; usually the bench "
                        "host logged off or rebooted under it",
}

NO_RETRY_STATUSES = {
    # Blocks the device instead: a run nobody can account for should not
    # silently start another one.
    "unreachable": "the agent never answered; needs acknowledgement",
    "cancelled": "stopped on request",
    "partial": "some boots were recorded; a retry risks losing them for a "
               "chance at more",
    "success": "nothing to retry",
}


@dataclass(frozen=True)
class RetryPlan:
    """What the scheduler should enqueue, and why."""

    reason: str                 # the short machine-readable cause
    detail: str                 # the human sentence, for the decision log
    stages: str = "all"         # stage string for the retry run
    recover_first: bool = True  # POST /recover before re-running

    @property
    def resume_pull(self) -> bool:
        return self.stages == "capture"


def plan_for(status, failure_stage=None, exit_code=None,
             error_class=None) -> RetryPlan | None:
    """The retry for one terminal outcome, or None to leave it alone.

    `status` is the run's own status; `failure_stage` and `exit_code` come
    from the status document. The stage wins when both are present -- it is
    the more specific signal -- and the exit code is only consulted when a
    run died without writing a status document at all. `error_class` is the
    last resort, for a run that died without writing one *or* reporting an
    exit code.
    """
    status = (status or "").strip().lower()
    stage = (failure_stage or "").strip().lower()

    if status in NO_RETRY_STATUSES:
        return None
    if status in RETRY_STATUSES:
        # A timeout has no meaningful stage: the job never reported one.
        # Recovery first, because a timeout mid-flash leaves the board in EDL.
        return RetryPlan(reason="timeout", detail=RETRY_STATUSES[status])
    if status != "failed":
        return None

    if not stage and exit_code is not None:
        stage = _stage_from_exit_code(exit_code)

    if stage in RESUME_STAGES:
        # No recover: the device is booted and reachable over adb, which is
        # the one thing this retry depends on. Power-cycling it into EDL
        # would delete the logs it is trying to collect.
        return RetryPlan(reason=f"retry_{stage}", detail=RESUME_STAGES[stage],
                         stages="capture", recover_first=False)
    if stage in RETRY_STAGES:
        return RetryPlan(reason=f"retry_{stage}", detail=RETRY_STAGES[stage])

    if not stage:
        # Recover first: the job was killed at an unknown point, which may
        # have been mid-flash with the board left sitting in EDL.
        detail = RETRY_ERROR_CLASSES.get((error_class or "").strip())
        if detail:
            return RetryPlan(reason="retry_no_status", detail=detail)
    return None


def _stage_from_exit_code(exit_code) -> str:
    """bootbench's EXIT_NAMES, without importing it.

    Mirrored rather than imported because the policy has to be evaluable on
    a coordinator whose `bootbench_path` does not resolve -- backfill and log
    repair are optional, deciding whether to retry a failure is not.
    tests/test_retry_policy.py asserts this table matches the skill's.
    """
    names = {
        0: "ok", 1: "unknown", 2: "usage", 10: "build_discovery", 11: "edl",
        12: "flash", 13: "serial_login", 14: "boot_collect", 15: "adb",
        16: "parse", 17: "record", 18: "tac", 130: "interrupted",
    }
    try:
        return names.get(int(exit_code), "")
    except (TypeError, ValueError):
        return ""


def describe(plan) -> str:
    if plan is None:
        return "no retry"
    return f"{plan.reason}: {plan.detail} (stages={plan.stages})"
