"""Alerting: the small number of things worth an email at 09:00.

A nightly that emails on every event trains everyone to filter it, so this
module is deliberately quiet. It alerts on four conditions, and nothing
else:

  1. A terminal failure with no retry left. A failure that will be retried
     in thirty minutes is not news yet.
  2. A `partial` run, on a quieter channel. A device that silently records
     five boots out of six instead of failing is the exact failure mode this
     whole system exists to catch, so it cannot be silent -- but it is also
     not an emergency.
  3. No successful run in `stale_success_days`. Catches the case no single
     event covers: nothing is failing because nothing is happening.
  4. Four consecutive ticks of `agent_unhealthy` or `discovery_failed`.
     One is a blip; an hour of them means the bench host is logged out.

Dedup is by `(device_id, reason)` with a per-severity cooldown, held in a
JSON file beside the artifacts rather than in a table. The state is worth
nothing if it is lost -- the worst case is one duplicate email -- and
keeping it out of the database means the "the database is unreachable"
alert can still be sent.

Everything here is best-effort and never raises into the caller: an SMTP
server that is down must not be able to stop a nightly from running.
"""

from __future__ import annotations

import json
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from ..db import queries
from ..utils.credential_manager import get_notify_credentials
from ..utils.logger import get_logger

log = get_logger("notify")

STATE_NAME = "alerts.json"

# How long the same (device, reason) stays quiet after being sent.
COOLDOWN = {
    "alert": timedelta(hours=6),
    "notice": timedelta(hours=24),
}

CONSECUTIVE_ERROR_THRESHOLD = 4


def _utcnow():
    return datetime.now(timezone.utc)


class Alert:
    """One thing worth telling someone, with the key that dedups it."""

    def __init__(self, device_id, reason, subject, body, severity="alert"):
        self.device_id = device_id
        self.reason = reason
        self.subject = subject
        self.body = body
        self.severity = severity

    @property
    def key(self) -> str:
        return f"{self.device_id}:{self.reason}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Alert {self.key} {self.severity}>"


# ---------------------------------------------------------------------------
# dedup state
# ---------------------------------------------------------------------------

def _state_path(settings) -> Path:
    return settings.artifact_path / STATE_NAME


def load_state(settings) -> dict:
    path = _state_path(settings)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Absent or corrupt is not an error: an empty state means the next
        # alert is sent, which is the safe direction to fail in.
        return {}


def save_state(settings, state) -> None:
    path = _state_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("cannot write %s: %s", path, exc)


def is_muted(state, alert, now=None) -> bool:
    now = now or _utcnow()
    sent = state.get(alert.key)
    if not sent:
        return False
    try:
        when = datetime.fromisoformat(sent)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now - when < COOLDOWN.get(alert.severity, COOLDOWN["alert"])


# ---------------------------------------------------------------------------
# deciding what to say
# ---------------------------------------------------------------------------

def collect(conn, inventory, settings, decisions=None, now=None) -> list:
    """Every alert the current state of the world justifies.

    Separated from sending so `main.py tick --dry-run` can print what it
    would have said, and so the whole decision is testable without an SMTP
    server.
    """
    now = now or _utcnow()
    alerts = []
    # A device this tick just enqueued work for is not a device to complain
    # about: the retry is in flight, and `last_run` has not caught up with it
    # yet because `apply` and this call share one transaction.
    enqueued = {d.device_id for d in (decisions or []) if d.enqueues}

    for device in sorted(inventory.enabled, key=lambda d: d.device_id):
        device_id = device.device_id
        if device_id in enqueued:
            continue
        last = queries.last_run(conn, device_id)
        state = queries.get_scheduler_state(conn, device_id)

        if last and last["status"] in ("failed", "timeout", "unreachable"):
            alert = _failure_alert(conn, device, last)
            if alert is not None:
                alerts.append(alert)
        elif last and last["status"] == "partial":
            alerts.append(Alert(
                device_id, f"partial_{last['run_id']}",
                f"[bootbench] {device_id}: partial run {last['run_id']}",
                f"Run {last['run_id']} of {device_id} exited cleanly but "
                f"recorded fewer boots than it attempted.\n\n"
                f"A device that quietly records five boots out of six is "
                f"degrading, not healthy. The run page lists the per-boot "
                f"parse errors.\n",
                severity="notice",
            ))

        stale = _stale_alert(conn, device, settings, now)
        if stale is not None:
            alerts.append(stale)

        if state and state.get("consecutive_errors", 0) >= CONSECUTIVE_ERROR_THRESHOLD:
            reason = state.get("last_reason") or "error"
            alerts.append(Alert(
                device_id, f"persistent_{reason}",
                f"[bootbench] {device_id}: {reason} for "
                f"{state['consecutive_errors']} consecutive ticks",
                f"The scheduler has been unable to evaluate or dispatch "
                f"{device_id} for {state['consecutive_errors']} ticks in a "
                f"row; the latest reason was {reason!r}.\n\n"
                f"If this is agent_unhealthy, the usual cause is that the "
                f"bench host rebooted and nobody logged back in: the agent "
                f"only runs in an interactive desktop session, so there is "
                f"no session for it to start in. Check /healthz for "
                f"session_id and tac_device_count.\n",
            ))

    return alerts


def _failure_alert(conn, device, last):
    """A failure is only news once no retry is coming."""
    from . import retry as retry_policy

    plan = retry_policy.plan_for(last["status"],
                                 failure_stage=last.get("failure_stage"),
                                 exit_code=last.get("exit_code"),
                                 error_class=last.get("error_class"))
    if plan is not None:
        depth = queries.retry_depth(conn, last["run_id"])
        if depth < device.max_retries:
            # A retry is owed. Staying quiet here is the difference between
            # one email a night and three.
            return None

    stage = last.get("failure_stage") or "unknown"
    body = [
        f"Run {last['run_id']} of {device.device_id} ended as "
        f"{last['status']}.",
        "",
        f"  build         {last.get('build_number')}",
        f"  failure stage {stage}",
        f"  exit code     {last.get('exit_code')}",
        f"  finished      {last.get('finished_at')}",
        "",
    ]
    if last["status"] == "unreachable":
        body.append(
            "The agent stopped answering and never came back, so what the "
            "board actually did is unknown. This device is BLOCKED: the "
            "scheduler will not enqueue another run until someone "
            "acknowledges this one on its run page.")
    elif stage == "flash":
        body.append(
            "A flash failure is deliberately not retried: the boot "
            "partition may be mid-write, and the next step is a human "
            "looking at the board rather than another PCAT invocation.")
    else:
        body.append(
            "No retry is scheduled -- either this failure is deterministic "
            "or the device has used its retries. See the run page for the "
            "mirrored runner.log.")

    return Alert(device.device_id, f"failed_{last['run_id']}",
                 f"[bootbench] {device.device_id}: run {last['run_id']} "
                 f"{last['status']} ({stage})",
                 "\n".join(body) + "\n")


def _stale_alert(conn, device, settings, now):
    """Nothing is failing because nothing is happening.

    Deliberately measured from the last *success*: a week of partials is
    exactly the state this catches, and a week of failures would already
    have alerted once and then gone quiet under its cooldown.
    """
    days = int(settings.stale_success_days or 0)
    if days <= 0:
        return None

    success = queries.last_success(conn, device.device_id)
    if success is None:
        # Never succeeded. Not alerted on: a device being set up has no
        # successes yet, and telling its owner so every six hours is noise.
        return None

    finished = success.get("finished_at")
    if finished is None:
        return None
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    age = now - finished
    if age < timedelta(days=days):
        return None

    return Alert(
        device.device_id, "stale",
        f"[bootbench] {device.device_id}: no successful run in "
        f"{age.days} day(s)",
        f"The last successful run of {device.device_id} was run "
        f"{success['run_id']} (build {success.get('build_number')}) on "
        f"{finished.isoformat()}, {age.days} day(s) ago.\n\n"
        f"Nothing has necessarily failed. The usual causes are a disabled "
        f"device, a bench host that is logged out, or a build share that "
        f"has stopped publishing master nightlies -- the /schedule page "
        f"shows which, per tick.\n",
    )


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------

def send(settings, alerts, *, dry_run=False, now=None) -> dict:
    """Send what is not muted, then record what was sent.

    Returns a summary rather than raising. An unreachable SMTP server is a
    reason to log a warning, not a reason for `tick` to exit non-zero and
    have cron mail someone about the mailer.
    """
    now = now or _utcnow()
    creds = get_notify_credentials()
    state = load_state(settings)

    sent, muted, failed = [], [], []
    for alert in alerts:
        if is_muted(state, alert, now):
            muted.append(alert.key)
            continue
        if dry_run:
            sent.append(alert.key)
            continue

        delivered = False
        if creds.get("smtp_host") and creds.get("notify_to"):
            delivered |= _send_email(creds, alert)
        if creds.get("notify_webhook"):
            delivered |= _post_webhook(creds["notify_webhook"], alert)

        if not delivered and not (creds.get("smtp_host")
                                  or creds.get("notify_webhook")):
            # No transport configured at all. Not an error -- plenty of
            # installations read the dashboard -- but the alert still goes to
            # the log, which systemd captures.
            log.warning("ALERT %s: %s", alert.key, alert.subject)
            delivered = True

        if delivered:
            state[alert.key] = now.isoformat()
            sent.append(alert.key)
        else:
            failed.append(alert.key)

    if not dry_run and sent:
        save_state(settings, state)
    return {"sent": sent, "muted": muted, "failed": failed}


def _send_email(creds, alert) -> bool:
    message = EmailMessage()
    message["Subject"] = alert.subject
    message["From"] = creds.get("smtp_from") or creds["smtp_user"] or \
        "bootbench@localhost"
    message["To"] = ", ".join(creds["notify_to"])
    message.set_content(alert.body)

    try:
        with smtplib.SMTP(creds["smtp_host"], creds["smtp_port"],
                          timeout=20) as smtp:
            if creds.get("smtp_user"):
                try:
                    smtp.starttls()
                except smtplib.SMTPException:
                    # A server without STARTTLS is a deployment choice, not a
                    # failure. Authenticating in the clear on a lab network is
                    # the documented posture.
                    pass
                smtp.login(creds["smtp_user"], creds.get("smtp_password") or "")
            smtp.send_message(message)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("email for %s not sent: %s", alert.key, exc)
        return False


def _post_webhook(url, alert) -> bool:
    payload = json.dumps({
        "device_id": alert.device_id, "reason": alert.reason,
        "severity": alert.severity, "subject": alert.subject,
        "text": alert.body,
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("webhook for %s not posted: %s", alert.key, exc)
        return False


def evaluate(conn, inventory, settings, decisions=None, *, dry_run=False,
             now=None) -> dict:
    """Collect and send in one call. The tail of `main.py tick`."""
    alerts = collect(conn, inventory, settings, decisions, now=now)
    summary = send(settings, alerts, dry_run=dry_run, now=now)
    summary["alerts"] = [
        {"key": a.key, "severity": a.severity, "subject": a.subject}
        for a in alerts
    ]
    return summary
