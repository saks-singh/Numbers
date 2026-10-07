"""The nightly decision: should this device run right now, and why not.

One crontab line calls `main.py tick` every fifteen minutes and this module
decides what that means for each device. The output is always a recorded
`scheduler_decision` row -- including for the boring outcomes -- because
"why didn't it run last night?" is the most common question anyone asks of a
nightly, and it is unanswerable from `run` rows alone: the whole point is
that no run was created.

Two design choices worth knowing:

  * **Catch-up, not wall-clock matching.** croniter is asked "was there a
    fire time in (last_tick_at, now]?", not "is it 01:00 right now". A
    window missed because the coordinator was down, or because a tick ran
    four minutes late, is still caught later the same day instead of
    silently skipped until tomorrow.

  * **Cheapest first; no network before the health check.** A tick that
    fires 96 times a day must cost nothing when there is nothing to do.
    Steps 1-5 are database-only. Only once a device genuinely wants to run
    does this module touch the bench host, and then it checks the agent is
    *capable* before committing -- so "the TAC is in session 0" is a clean
    skip with a reason at 01:00 rather than a failed flash at 01:40.

The order deviates from the obvious one in exactly one place: the retry
check runs before the not-due check. A retry exists to happen 30 minutes
after a transient failure, not at the next cron window, so making it wait
for one would defeat `retry_delay_minutes` entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..db import queries
from ..runner.agent_client import AgentError, AgentUnreachable, assess_health
from ..utils.logger import get_logger
from . import retry as retry_policy

log = get_logger("scheduler")

# Every reason this module can record, with the action it carries. Spelled
# out so the dashboard can explain one without a lookup elsewhere, and so a
# typo'd reason string is a missing key rather than a mystery row.
REASONS = {
    "disabled": ("skipped", "the device is disabled in devices.yaml"),
    "first_tick": ("skipped", "first time this device was evaluated; the "
                              "schedule starts from now"),
    "overlap": ("skipped", "a run is already queued or running"),
    "unreachable_unacknowledged": (
        "blocked", "the last run ended with the agent unreachable; a human "
                   "must acknowledge it before this device runs again"),
    "not_due": ("skipped", "no scheduled fire time since the last tick"),
    "agent_unhealthy": ("error", "the bench host cannot run a job right now"),
    "discovery_failed": ("error", "could not ask the agent what the latest "
                                  "build is"),
    "image_not_ready": ("skipped", "a new build exists but this target's "
                                   "image has not landed yet"),
    "unparseable_build": ("skipped", "the build folder is not a master "
                                     "nightly, so it has no build number"),
    "already_benchmarked": ("skipped", "this build has already been measured "
                                       "on this device"),
    "new_build": ("enqueued", "a new build is ready"),
    "forced": ("enqueued", "--force was given"),
    "retry_pending": ("skipped", "a retry is due, but not yet"),
    "retries_exhausted": ("skipped", "the last failure is retryable but this "
                                     "device has used its retries"),
    # The enqueued retries. Built directly rather than through _decision(),
    # but listed so the dashboard can explain every reason it may render.
    "timeout": ("enqueued", "retrying a run that exceeded its budget"),
    "retry_tac": ("enqueued", "retrying a TAC that did not enumerate"),
    "retry_edl": ("enqueued", "retrying a device that never entered EDL"),
    "retry_serial_login": ("enqueued", "retrying a missed console login"),
    "retry_boot_collect": ("enqueued", "retrying a boot that did not finish"),
    "retry_adb": ("enqueued", "resuming the pull; the logs are already on "
                              "the device"),
}


@dataclass(frozen=True)
class Decision:
    """What to do about one device, and the sentence explaining it."""

    device_id: str
    action: str
    reason: str
    build: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)
    run_id: int | None = None
    stages: str | None = None
    parent_run_id: int | None = None
    trigger_source: str = "cron"
    recover_first: bool = False

    @property
    def enqueues(self) -> bool:
        return self.action == "enqueued"

    def describe(self) -> str:
        _action, sentence = REASONS.get(self.reason, (self.action, self.reason))
        return f"{self.action}/{self.reason}: {sentence}"


def _decision(device_id, reason, **kwargs) -> Decision:
    action = REASONS[reason][0]
    return Decision(device_id=device_id, action=action, reason=reason, **kwargs)


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------

def _croniter():
    try:
        from croniter import croniter
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise RuntimeError(
            "croniter is not installed; `pip install -r requirements.txt`. "
            "Without it the schedule cannot be evaluated and the nightly "
            "would silently never fire."
        ) from exc
    return croniter


def schedule_is_valid(expression) -> bool:
    croniter = _croniter()
    try:
        return bool(croniter.is_valid(str(expression)))
    except Exception:
        return False


def was_due(expression, last_tick, now) -> bool:
    """Did a fire time fall in (last_tick, now]?

    Half-open on the left so a fire time exactly at `last_tick` is not
    counted twice -- otherwise a tick that lands precisely on 01:00 would
    enqueue, and the next one would enqueue again for the same window.
    """
    croniter = _croniter()
    if last_tick is None:
        return False
    last_tick = _aware(last_tick)
    now = _aware(now)
    if now <= last_tick:
        return False
    nxt = croniter(str(expression), last_tick).get_next(datetime)
    return _aware(nxt) <= now


def _aware(value):
    """Everything here compares TIMESTAMPTZ against `now`, so a naive
    datetime from anywhere is a bug waiting for a DST boundary."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

def decide(conn, device, *, settings, now=None, runner=None, force=False,
           health=None, health_error=None, latest=None) -> Decision:
    """Evaluate one device. Pure: records nothing, enqueues nothing.

    `runner` is only consulted from step 6 onwards. `health`/`health_error`
    let a caller supply an answer it already has -- the tick shares one
    `/healthz` across every device on the same agent rather than asking once
    per board -- and passing the *error* rather than pre-empting the
    decision keeps the cheap checks ahead of it in the order.
    """
    now = _aware(now or datetime.now(timezone.utc))
    device_id = device.device_id

    # 1. Disabled. Cheapest possible answer and the most common deliberate one.
    if not device.enabled:
        return _decision(device_id, "disabled")

    # 2. Overlap. A queued run from the last tick, or a manual trigger
    #    someone pressed a minute ago, both mean "not now".
    active = queries.active_run(conn, device_id)
    if active is not None:
        return _decision(device_id, "overlap",
                         detail={"run_id": active["run_id"],
                                 "status": active["status"]})

    last = queries.last_run(conn, device_id)

    # 3. Blocked. An unreachable run is the one outcome that needs a human:
    #    nobody knows what the board did, so the next run's numbers cannot be
    #    trusted as a comparison until someone has looked.
    if (last and last["status"] == "unreachable"
            and last.get("acknowledged_at") is None):
        return _decision(device_id, "unreachable_unacknowledged",
                         detail={"run_id": last["run_id"]})

    # 4. A retry that is owed. Evaluated before the due check on purpose: a
    #    retry is scheduled relative to the failure, not to the cron
    #    expression, so making it wait for the next window would defeat
    #    retry_delay_minutes. --force skips this path entirely -- someone
    #    asking for a run now wants a full one, not a resumed pull.
    if not force:
        retry = _retry_decision(conn, device, last, now, settings)
        if retry is not None:
            return _health_gate(device, retry, runner=runner, health=health,
                                health_error=health_error)

    # 5. Due? croniter against the recorded tick, so a missed window is
    #    caught rather than lost.
    state = queries.get_scheduler_state(conn, device_id)
    last_tick = _aware(state["last_tick_at"]) if state else None
    if not force:
        if last_tick is None:
            # Never evaluated. Seeding the clock rather than firing avoids a
            # flash the instant someone installs the crontab at 14:00.
            return _decision(device_id, "first_tick")
        if not was_due(device.schedule, last_tick, now):
            return _decision(device_id, "not_due",
                             detail={"schedule": device.schedule,
                                     "last_tick_at": last_tick.isoformat()})

    # 6. Is the bench host capable? The first network call of the tick.
    if health_error is not None:
        return _decision(device_id, "agent_unhealthy",
                         detail={"error": health_error})
    if health is None and runner is not None:
        try:
            health = runner.healthz()
        except (AgentUnreachable, AgentError) as exc:
            return _decision(device_id, "agent_unhealthy",
                             detail={"error": str(exc)})
    if health is not None:
        ok, reasons = assess_health(health)
        if not ok:
            return _decision(device_id, "agent_unhealthy",
                             detail={"reasons": reasons,
                                     "session_id": health.get("session_id"),
                                     "tac_device_count":
                                         health.get("tac_device_count")})

    # 7. What is the newest build? Cheap: one SMB directory listing on the
    #    bench host, no hardware touched.
    if latest is None:
        if runner is None:
            return _decision(device_id, "discovery_failed",
                             detail={"error": "no runner available"})
        try:
            latest = runner.latest_build(device_id)
        except (AgentUnreachable, AgentError) as exc:
            return _decision(device_id, "discovery_failed",
                             detail={"error": str(exc)})

    build = dict(latest.get("build") or latest or {})

    # 8. The common not-a-failure: the nightly published, but this target's
    #    image has not finished building. Retried next tick for free.
    if build.get("target_image_ready") is False:
        return _decision(device_id, "image_not_ready", build=build,
                         detail={"build_dir": build.get("build_dir")})

    # 9. Only `..._Nightly_Build_master_<n>` parses. A branch or release
    #    build is invisible to the skill's regex, and saying so beats
    #    recording a run with a NULL build number nobody can trend.
    if build.get("build_number") is None:
        return _decision(device_id, "unparseable_build", build=build,
                         detail={"build_folder": build.get("build_folder")})

    # 10. Already measured. A query, not a constraint: --force re-runs the
    #     same build deliberately, to measure run-to-run variance.
    if not force and queries.is_build_benchmarked(conn, device_id,
                                                  build["build_number"]):
        return _decision(device_id, "already_benchmarked", build=build)

    return _decision(device_id, "forced" if force else "new_build",
                     build=build, stages=device.stages)


def _health_gate(device, decision, *, runner, health, health_error):
    """Apply step 6 to a retry that skipped steps 5 and 7-10.

    A retry still must not be enqueued against a bench host that cannot run
    it -- that is how a retry storm against a logged-out machine starts. A
    retry that is merely *pending* is returned untouched: reporting
    "unhealthy" for a decision that was going to wait anyway would make the
    decision log lie.
    """
    if not decision.enqueues:
        return decision
    if health_error is not None:
        return _decision(device.device_id, "agent_unhealthy",
                         detail={"error": health_error, "for": "retry"})
    if health is None and runner is not None:
        try:
            health = runner.healthz()
        except (AgentUnreachable, AgentError) as exc:
            return _decision(device.device_id, "agent_unhealthy",
                             detail={"error": str(exc), "for": "retry"})
    if health is not None:
        ok, reasons = assess_health(health)
        if not ok:
            return _decision(device.device_id, "agent_unhealthy",
                             detail={"reasons": reasons, "for": "retry"})
    return decision


def _retry_decision(conn, device, last, now, settings):
    """Is a retry of the last failure owed, and is it owed yet?

    None means "the last run gives no reason to retry", which is the normal
    case and must not be confused with "a retry is due but waiting".
    """
    if not last or last["status"] in ("success", "queued", "running"):
        return None
    if device.max_retries <= 0:
        return None

    plan = retry_policy.plan_for(last["status"],
                                 failure_stage=last.get("failure_stage"),
                                 exit_code=last.get("exit_code"),
                                 error_class=last.get("error_class"))
    if plan is None:
        return None

    depth = queries.retry_depth(conn, last["run_id"])
    if depth >= device.max_retries:
        return _decision(device.device_id, "retries_exhausted",
                         detail={"run_id": last["run_id"], "attempts": depth,
                                 "max_retries": device.max_retries,
                                 "plan": plan.reason})

    finished = _aware(last.get("finished_at"))
    delay = timedelta(minutes=max(0, int(settings.retry_delay_minutes)))
    if finished is not None and now < finished + delay:
        # Deliberately not "retry immediately": a TAC that did not enumerate
        # thirty seconds ago will not enumerate now, and an immediate retry
        # just burns the attempt.
        return _decision(device.device_id, "retry_pending",
                         detail={"run_id": last["run_id"],
                                 "due_at": (finished + delay).isoformat(),
                                 "plan": plan.reason})

    return Decision(
        device_id=device.device_id, action="enqueued", reason=plan.reason,
        stages=plan.stages, parent_run_id=last["run_id"],
        trigger_source="retry", recover_first=plan.recover_first,
        detail={"parent_run_id": last["run_id"], "why": plan.detail,
                "attempt": depth + 1, "max_retries": device.max_retries},
    )


# ---------------------------------------------------------------------------
# applying a decision
# ---------------------------------------------------------------------------

def apply(conn, device, decision, *, runner=None, dry_run=False) -> Decision:
    """Record the decision and, if it enqueues, create the run.

    Both writes happen in the caller's transaction, so a crash between them
    cannot leave an enqueued run with no audit row or an audit row claiming a
    run that does not exist.
    """
    run_id = None
    if decision.enqueues and not dry_run:
        if decision.recover_first and runner is not None:
            # A retry after a timeout or an EDL failure starts by putting the
            # board back in a state where a flash can begin at all.
            _recover(runner, device)
        run_id = queries.enqueue_run(
            conn, device,
            trigger_source=decision.trigger_source,
            triggered_by="scheduler",
            build=decision.build or None,
            parent_run_id=decision.parent_run_id,
            stages=decision.stages,
        )

    if not dry_run:
        queries.record_decision(
            conn, device.device_id, decision.action, decision.reason,
            build_number=(decision.build or {}).get("build_number"),
            run_id=run_id, detail=decision.detail or None,
        )
        queries.touch_scheduler_state(
            conn, device.device_id, tick_at=None,
            action=decision.action, reason=decision.reason,
            errored=decision.action == "error",
        )

    return Decision(
        device_id=decision.device_id, action=decision.action,
        reason=decision.reason, build=decision.build, detail=decision.detail,
        run_id=run_id, stages=decision.stages,
        parent_run_id=decision.parent_run_id,
        trigger_source=decision.trigger_source,
        recover_first=decision.recover_first,
    )


def _recover(runner, device) -> None:
    try:
        runner.recover(device.device_id)
        log.info("%s: recovery before retry", device.device_id)
    except (AgentUnreachable, AgentError) as exc:
        # Not fatal: the run itself will fail at the EDL stage and say so,
        # which is a better signal than refusing to retry at all.
        log.warning("%s: recovery before retry failed: %s",
                    device.device_id, exc)


def tick(conn, inventory, *, settings, runner_for=None, force=False,
         dry_run=False, now=None, only=None) -> list:
    """Evaluate every device once. The body of `main.py tick`.

    `/healthz` and `/latest-build` are asked once per *agent*, not once per
    device, because two boards on one bench host share both answers and a
    four-board host would otherwise make four identical calls every fifteen
    minutes.
    """
    now = _aware(now or datetime.now(timezone.utc))
    devices = [d for d in sorted(inventory, key=lambda d: d.device_id)
               if only is None or d.device_id in only]

    health_cache: dict = {}
    decisions = []
    for device in devices:
        runner = runner_for(device) if runner_for else None

        health, health_error = None, None
        if device.enabled and runner is not None:
            if device.agent_url not in health_cache:
                try:
                    health_cache[device.agent_url] = (runner.healthz(), None)
                except (AgentUnreachable, AgentError) as exc:
                    health_cache[device.agent_url] = (None, str(exc))
            health, health_error = health_cache[device.agent_url]

        decision = decide(conn, device, settings=settings, now=now,
                          runner=runner, force=force, health=health,
                          health_error=health_error)
        decision = apply(conn, device, decision, runner=runner,
                         dry_run=dry_run)
        decisions.append(decision)

        if decision.enqueues:
            log.info("%s: enqueued run %s (%s, build %s)", device.device_id,
                     decision.run_id, decision.reason,
                     (decision.build or {}).get("build_number"))
        else:
            log.info("%s: %s", device.device_id, decision.describe())

    return decisions
