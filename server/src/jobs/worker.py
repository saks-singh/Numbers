"""The worker: one claimed run, driven to a terminal state.

Runs as its own process (`bootbench-worker.service`), never as a thread in
the web app. A nightly `all` run is forty minutes of flashing and booting,
and `systemctl restart bootbench-web` must not be able to interrupt it.

The loop is deliberately boring:

    claim a queued run        (FOR UPDATE SKIP LOCKED + an advisory lock)
    POST /jobs                (idempotent on job_id == run_id)
    poll GET /jobs/<id>       every poll_interval, mirroring the log
    on terminal state         fetch the manifest, download, ingest, finish
    release the device

Three properties are worth stating because they are what make it safe to
restart this process at any moment:

  * The job is not a child of this process, or of a network connection. A
    dropped request is a dropped poll. Killing the worker mid-flash loses
    the *mirror* of the log, not the flash.

  * `POST /jobs` is idempotent on `job_id`, and `job_id` is the `run_id`, so
    a re-submit after a timeout or a restart returns the running job rather
    than starting a second one. This is the property that makes retrying a
    submit safe at all.

  * The device lock is a session-level advisory lock on this worker's own
    connection, so a worker that dies releases it. A status column or a lock
    table would wedge the device until someone noticed.

What this module deliberately does NOT do is enqueue the retry for a
failure it just saw. See scheduler/retry.py: honouring `retry_delay_minutes`
here would mean sleeping while holding a device lock, and the scheduler is
already awake every fifteen minutes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ..db import ingest, pool, queries
from ..runner import artifacts
from ..runner.agent_client import (
    AgentError, AgentUnreachable, client_for,
)
from ..runner.base import is_terminal, job_timeout_seconds
from ..utils.logger import get_logger

log = get_logger("worker")

# The file the web app's /healthz stats to decide whether a worker is alive.
HEARTBEAT_NAME = "worker.heartbeat"


def _utcnow():
    return datetime.now(timezone.utc)


class Worker:
    """Claims and executes runs. One instance per process.

    `runner_factory` exists for tests and for `runner_backend: local_fake`:
    everything below talks to the `Runner` protocol, never to HTTP directly.
    """

    def __init__(self, settings, inventory, *, runner_factory=None,
                 sleep=time.sleep, now=time.time):
        self.settings = settings
        self.inventory = inventory
        self._sleep = sleep
        self._now = now
        self._runner_factory = runner_factory or self._default_runner
        self._runners: dict = {}
        # Devices this worker could not lock on this pass. Cleared every
        # poll cycle: another worker's job finishes eventually, and a
        # permanently excluded device would look exactly like a stuck queue.
        self._deferred: set = set()

    # -- runners ---------------------------------------------------------
    def _default_runner(self, device):
        if self.settings.runner_backend == "local_fake":
            # Only reachable if someone configured it on a real coordinator;
            # the tests inject their own factory. Fail loudly rather than
            # silently benchmarking nothing.
            raise RuntimeError(
                "runner_backend is 'local_fake' but no fixture root was "
                "supplied; pass runner_factory= or set runner_backend: agent")
        return client_for(device)

    def runner_for(self, device):
        if device.agent_url not in self._runners:
            self._runners[device.agent_url] = self._runner_factory(device)
        return self._runners[device.agent_url]

    # -- the loop --------------------------------------------------------
    def serve_forever(self, max_runs=None, stop=None) -> int:
        """Claim and execute runs until told to stop.

        `max_runs` and `stop` are what make this testable and what make
        `--once` possible; a plain `while True` would be neither.
        """
        executed = 0
        log.info("worker starting: backend=%s poll=%ss claim=%ss",
                 self.settings.runner_backend, self.settings.poll_interval,
                 self.settings.claim_interval)

        # Before claiming anything new: pick up whatever a previous worker
        # process was watching when it stopped. A restart during a job must
        # not cost the run.
        self.heartbeat()
        try:
            self.adopt_running()
        except pool.DatabaseError as exc:
            log.error("cannot look for runs to adopt: %s", exc)

        while True:
            if stop is not None and stop():
                break
            if max_runs is not None and executed >= max_runs:
                break

            self.heartbeat()
            try:
                result = self.run_once()
            except pool.DatabaseError as exc:
                # The database being briefly unavailable is not a reason to
                # exit: systemd would restart us into the same condition at a
                # faster rate than this backoff.
                log.error("database unavailable: %s", exc)
                self._sleep(self.settings.claim_interval)
                continue

            if result is None:
                self._deferred.clear()
                self._wait_for_work()
                continue
            executed += 1
        return executed

    def _wait_for_work(self) -> None:
        """Sleep until the next claim attempt.

        LISTEN/NOTIFY would wake us the instant a manual trigger lands, and
        `enqueue_run` already sends the notification. It is not used here:
        waiting on a notification means holding a connection parked in a
        blocking read, and the whole point of `claim_interval` being ten
        seconds is that a ten-second worst case on a forty-minute job does
        not matter. The NOTIFY stays in place for a future long-poll mode.
        """
        self._sleep(self.settings.claim_interval)

    def heartbeat(self) -> Path:
        """Touch a file the web app's /healthz reads.

        A file rather than a table: the worker and the web app are separate
        processes on the same host by design, and this costs no schema, no
        migration, and no write to a database the dashboard may already be
        unable to reach -- which is exactly when you want to know whether
        the worker is alive.
        """
        path = self.settings.artifact_path / HEARTBEAT_NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_utcnow().isoformat(), encoding="utf-8")
        except OSError as exc:
            log.warning("cannot write heartbeat %s: %s", path, exc)
        return path

    # -- adoption --------------------------------------------------------
    def adopt_running(self) -> int:
        """Resume runs a previous worker process left in `running`.

        `claim_next_run` only ever looks at `queued`, so a run that was
        already dispatched when this process started is invisible to it. The
        job itself is unaffected -- it is a child of the agent, not of us --
        so the recovery is simply to start polling it again, which is why
        this hands straight to the same `_poll`/`_finalize` pair a fresh run
        uses. An already-finished job is read, ingested and closed on the
        first poll, because `_poll` checks for a terminal state before it
        checks the deadline.
        """
        with pool.connection() as conn:
            stale = queries.adoptable_runs(conn)
        if not stale:
            return 0

        log.info("adopting %s run(s) left in 'running' by a previous worker",
                 len(stale))
        return sum(1 for row in stale if self._adopt(row))

    def _adopt(self, row) -> bool:
        """Drive one abandoned run to a terminal status.

        Every failure here is contained to this run and recorded on it. A
        run we cannot adopt must not be left to take the worker down on
        every restart -- that turns one lost run into a crash loop.
        """
        run_id = row["run_id"]
        device_id = row["device_id"]

        with pool.connection() as conn:
            if not queries.try_lock_device(conn, device_id):
                log.info("run %s: not adopting, another worker holds %s",
                         run_id, device_id)
                return False
            try:
                device = self.inventory.get(device_id)
                if device is None:
                    log.error("run %s names unknown device %s",
                              run_id, device_id)
                    queries.finish_run(
                        conn, run_id, "failed", error_class="ConfigError",
                        error_message=f"device {device_id} is not in "
                                      f"devices.yaml")
                    conn.commit()
                    return False

                runner = self.runner_for(device)
                run_dir = self.settings.run_dir(run_id)
                run_dir.mkdir(parents=True, exist_ok=True)
                queries.set_run_fields(conn, run_id, readopted=True)
                conn.commit()

                budget = job_timeout_seconds(device.num_boots,
                                             device.boot_timeout)
                started = row.get("started_at")
                elapsed = (_utcnow() - started).total_seconds() if started else 0.0
                # Whatever is left of the original budget, but never zero:
                # one poll has to happen, or a job that finished while we
                # were gone would be declared timed out instead of ingested.
                remaining = max(budget - elapsed, self.settings.poll_interval)

                log.info("run %s: adopting %s, %.0fs into a %ss budget",
                         run_id, device_id, elapsed, budget)
                job, outcome = self._poll(conn, run_id, device, runner,
                                          self._now() + remaining)
                self._finalize(conn, row, device, runner, job, outcome)
                return True
            except Exception as exc:
                conn.rollback()
                log.exception("run %s: adoption failed", run_id)
                queries.finish_run(conn, run_id, "failed",
                                   error_class=type(exc).__name__,
                                   error_message=f"adoption failed: {exc}")
                conn.commit()
                return False
            finally:
                queries.unlock_device(conn, device_id)
                conn.commit()

    def run_once(self):
        """Claim one run and drive it to completion. None if the queue is
        empty (or everything in it belongs to a device we cannot lock)."""
        with pool.connection() as conn:
            run = self._claim(conn)
            if run is None:
                return None

            run_id = run["run_id"]
            device = self.inventory.get(run["device_id"])
            try:
                if device is None:
                    # The run outlived its devices.yaml entry. Terminal, not
                    # retryable, and explicit: a run stuck in `running`
                    # forever would block the device it names.
                    log.error("run %s names unknown device %s",
                              run_id, run["device_id"])
                    queries.finish_run(
                        conn, run_id, "failed",
                        error_class="ConfigError",
                        error_message=f"device {run['device_id']} is not in "
                                      f"devices.yaml")
                    conn.commit()
                    return {"run_id": run_id, "status": "failed"}

                return self._execute(conn, run, device)
            finally:
                queries.unlock_device(conn, run["device_id"])
                conn.commit()

    def _claim(self, conn):
        """Claim a run whose device we can also lock.

        Two locks, deliberately. The row lock stops two workers taking the
        same run; the device lock stops two *runs* of the same device
        overlapping, which the queue alone cannot prevent -- a manual
        trigger and a nightly can legitimately both be queued.
        """
        while True:
            run = queries.claim_next_run(conn, exclude_devices=self._deferred)
            if run is None:
                conn.rollback()
                return None

            if queries.try_lock_device(conn, run["device_id"]):
                conn.commit()
                log.info("run %s claimed: %s build %s stages %s",
                         run["run_id"], run["device_id"],
                         run.get("build_number"), run.get("stages_requested"))
                return run

            # Someone else owns the device. Put the claim back and look for
            # work on another device rather than spinning on this one.
            conn.rollback()
            log.info("run %s deferred: another worker holds %s",
                     run["run_id"], run["device_id"])
            self._deferred.add(run["device_id"])

    # -- one run ---------------------------------------------------------
    def _execute(self, conn, run, device):
        run_id = run["run_id"]
        runner = self.runner_for(device)
        run_dir = self.settings.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        queries.set_run_fields(conn, run_id, agent_job_id=str(run_id),
                               artifact_dir=str(run_dir))
        conn.commit()

        stages = list(run.get("stages_requested") or
                      ["flash", "capture", "report"])
        request = {
            "stages": stages,
            "num_boots": device.num_boots,
            "boot_timeout": device.boot_timeout,
            # Only a retry resumes a pull. A hand-triggered capture-only run
            # is asking for a fresh capture and must boot the board; deriving
            # resume_pull from the stage list alone would silently turn it
            # into "re-pull whatever happens to be in /data".
            "resume_pull": (run.get("trigger_source") == "retry"
                            and stages == ["capture"]),
        }

        job, disposition = self._submit(conn, run_id, device, runner, request)
        if disposition != "submitted":
            return {"run_id": run_id, "status": disposition}

        deadline = self._now() + job_timeout_seconds(
            device.num_boots, device.boot_timeout)
        job, outcome = self._poll(conn, run_id, device, runner, deadline)
        return self._finalize(conn, run, device, runner, job, outcome)

    def _submit(self, conn, run_id, device, runner, request):
        """POST the job.

        Returns `(job, disposition)`. A disposition of "queued" means the run
        was put back for a later attempt; "failed" means it is already
        terminal and there is nothing left to poll.
        """
        try:
            response = runner.submit_job(run_id, device.device_id, **request)
        except AgentUnreachable as exc:
            # Nothing started, so requeueing is strictly safe -- and would be
            # safe even if it had, because submit is idempotent on job_id.
            log.warning("run %s: agent unreachable at submit: %s", run_id, exc)
            self._requeue(conn, run_id)
            return None, "queued"
        except AgentError as exc:
            if exc.status == 409:
                busy = (exc.payload or {}).get("job_id")
                log.warning("run %s: %s is busy with job %s; requeued",
                            run_id, device.device_id, busy)
                self._requeue(conn, run_id)
                self._deferred.add(device.device_id)
                return None, "queued"
            # 404 on an unknown device_id, 400 on a malformed request: a
            # config bug, and a retry at 01:15 would hit it again.
            log.error("run %s: agent refused the job: %s", run_id, exc)
            queries.finish_run(conn, run_id, "failed",
                               error_class=type(exc).__name__,
                               error_message=str(exc))
            conn.commit()
            return None, "failed"

        job = response.get("job") or {}
        if not response.get("created", True):
            # The agent already had this job: a previous attempt submitted it
            # and we lost the answer. Adopt it rather than failing.
            log.info("run %s: agent already had this job (state=%s); adopting",
                     run_id, job.get("state"))
            queries.set_run_fields(conn, run_id, readopted=True)
        queries.set_run_fields(conn, run_id, last_output_at=_utcnow())
        conn.commit()
        return job, "submitted"

    def _requeue(self, conn, run_id) -> None:
        queries.set_run_fields(conn, run_id, status="queued", started_at=None)
        conn.commit()

    def _poll(self, conn, run_id, device, runner, deadline):
        """Poll until terminal, timed out, or persistently unreachable.

        Returns `(job_or_None, outcome)` where outcome is "terminal",
        "timeout", or "unreachable".
        """
        offset = self._log_size(run_id)
        backoff = self.settings.poll_interval
        unreachable_since = None
        cancel_sent = False
        job = None

        while True:
            try:
                payload = runner.get_job(run_id)
                job = payload.get("job") or {}
                backoff = self.settings.poll_interval
                if unreachable_since is not None:
                    log.info("run %s: agent reachable again", run_id)
                    unreachable_since = None
            except AgentUnreachable as exc:
                # Not an emergency. The job on the bench host is a child of
                # the agent, not of this connection, and it keeps running.
                if unreachable_since is None:
                    unreachable_since = self._now()
                    log.warning("run %s: agent unreachable, backing off: %s",
                                run_id, exc)
                if self._now() >= deadline:
                    return job, "unreachable"
                self._sleep(min(backoff, self.settings.agent_backoff_max))
                backoff = min(backoff * 2, self.settings.agent_backoff_max)
                continue
            except AgentError as exc:
                if exc.status == 404:
                    # The agent forgot the job: its artifact tree was pruned,
                    # or it is a different agent than the one we submitted to.
                    log.error("run %s: agent has no such job", run_id)
                    return job, "lost"
                log.warning("run %s: polling error: %s", run_id, exc)
                self._sleep(self.settings.poll_interval)
                continue

            offset = self._mirror_log(run_id, runner, offset)
            if offset is not None:
                queries.set_run_fields(conn, run_id, last_output_at=_utcnow())
                conn.commit()

            if not cancel_sent and self._cancel_requested(conn, run_id):
                cancel_sent = True
                self._forward_cancel(run_id, runner)

            state = job.get("state") or ""
            if is_terminal(state):
                return job, "terminal"

            if self._now() >= deadline:
                log.error("run %s: exceeded its budget (%ss); the board may "
                          "be wedged", run_id,
                          job_timeout_seconds(device.num_boots,
                                              device.boot_timeout))
                return job, "timeout"

            self._sleep(self.settings.poll_interval)

    def _cancel_requested(self, conn, run_id) -> bool:
        row = queries.run_detail(conn, run_id)
        return bool(row and row.get("cancel_requested"))

    def _forward_cancel(self, run_id, runner) -> None:
        """Ask the agent to stop at a safe point.

        The agent writes a sentinel the skill checks between boots and before
        flashing, and refuses to do anything mid-flash. Interrupting PCAT
        while it writes a boot partition is not a cancellation, it is damage.
        """
        try:
            response = runner.cancel(run_id)
        except AgentError as exc:
            log.warning("run %s: cancel not delivered: %s", run_id, exc)
            return
        if response.get("cancelled"):
            log.info("run %s: cancel requested; it takes effect at the next "
                     "safe point", run_id)
        else:
            log.info("run %s: cancel refused: %s", run_id,
                     response.get("reason"))

    # -- log mirroring ---------------------------------------------------
    def _log_path(self, run_id) -> Path:
        return self.settings.run_dir(run_id) / "runner.log"

    def _log_size(self, run_id) -> int:
        path = self._log_path(run_id)
        return path.stat().st_size if path.is_file() else 0

    def _mirror_log(self, run_id, runner, offset):
        """Append whatever the agent has produced since `offset`.

        Mirrored locally for two reasons: `/runs/<id>/log` keeps working
        after the agent prunes its own job directories, and the dashboard's
        log pane then uses one byte-offset mechanism end to end -- agent to
        coordinator to browser -- instead of two that can disagree.

        Returns the new offset, or None if nothing arrived.
        """
        try:
            chunk = runner.tail_log(run_id, offset=offset)
        except AgentError as exc:
            log.debug("run %s: log tail failed: %s", run_id, exc)
            return None

        text = chunk.get("chunk") or ""
        if not text:
            return None

        path = self._log_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append as bytes: the offset the agent reports is a byte offset, and
        # treating it as a character count would drift on any non-ASCII line
        # PCAT happens to print.
        data = text.encode("utf-8", errors="replace")
        with path.open("ab") as handle:
            handle.write(data)
        return int(chunk.get("offset") or offset) + len(data)

    # -- finalization ----------------------------------------------------
    def _finalize(self, conn, run, device, runner, job, outcome):
        run_id = run["run_id"]
        run_dir = self.settings.run_dir(run_id)

        # One last tail, so the mirrored log contains the failure that ended
        # the job rather than stopping a poll short of it.
        self._mirror_log(run_id, runner, self._log_size(run_id))

        summary = self._fetch_artifacts(run_id, device, runner, run_dir)
        doc = self._status_document(job, run_dir)

        if outcome == "timeout":
            status = self._finish_without_status(
                conn, run_id, "timeout", job,
                "the job exceeded its budget", doc)
            self._recover(run_id, device, runner, cancel_first=True)
            return {"run_id": run_id, "status": status, "artifacts": summary}

        if outcome == "unreachable":
            # Blocks the device: the next tick refuses to enqueue until a
            # human acknowledges it. A run nobody can account for must not
            # silently become the baseline for the next comparison.
            status = self._finish_without_status(
                conn, run_id, "unreachable", job,
                "the agent stopped answering and never came back", doc)
            return {"run_id": run_id, "status": status, "artifacts": summary}

        if outcome == "lost" and doc is None:
            # The agent answered, but no longer knows this job -- it was
            # pruned, or we are talking to a different agent than the one
            # that ran it. Distinguished from a crash because the run may
            # well have succeeded and the evidence is simply gone.
            status = self._finish_without_status(
                conn, run_id, "failed", job,
                "the agent no longer knows this job; its artifacts were "
                "pruned or the agent_url now points elsewhere", doc)
            return {"run_id": run_id, "status": status, "artifacts": summary}

        if doc is None:
            status = self._finish_without_status(
                conn, run_id, "failed", job,
                "the job ended without writing a status document", doc)
            return {"run_id": run_id, "status": status, "artifacts": summary}

        try:
            ingested = ingest.ingest_status(
                conn, run_id, doc, artifact_dir=str(run_dir),
                readopted=bool(run.get("readopted")) or job.get("readopted") is True)
            queries.finish_run(conn, run_id, ingested["status"],
                               finished_at=ingest._utc(doc.get("ended_utc")))
            conn.commit()
        except ingest.IngestError as exc:
            conn.rollback()
            # The run happened; we just cannot model it. Record that plainly
            # instead of leaving the row in `running` forever.
            log.error("run %s: ingest failed: %s", run_id, exc)
            queries.finish_run(conn, run_id, "failed",
                               error_class="IngestError",
                               error_message=str(exc))
            conn.commit()
            return {"run_id": run_id, "status": "failed",
                    "artifacts": summary, "error": str(exc)}

        log.info("run %s: %s, %s/%s boots recorded%s", run_id,
                 ingested["status"], ingested["recorded"], ingested["boots"],
                 f", {len(ingested['parse_errors'])} parse error(s)"
                 if ingested["parse_errors"] else "")

        if ingested["status"] == "failed":
            # A failed flash can leave the board in EDL, where nothing else
            # can reach it -- including the next nightly.
            stage = (doc.get("failure_stage") or "").lower()
            if stage in ("edl", "flash", "tac"):
                self._recover(run_id, device, runner, cancel_first=False)

        return {"run_id": run_id, "status": ingested["status"],
                "artifacts": summary, "ingested": ingested}

    def _finish_without_status(self, conn, run_id, status, job, message, doc):
        """Terminal state for a run with no usable status document.

        Whatever the agent's job record knows is still recorded -- exit code,
        pid, its own error string -- because at 9 a.m. that is the whole
        evidence base.
        """
        fields = {
            "error_class": "NoStatusDocument" if doc is None else "RunFailed",
            "error_message": message,
        }
        if job:
            exit_code = job.get("exit_code")
            if exit_code is not None:
                fields["exit_code"] = int(exit_code)
            if job.get("error"):
                fields["error_message"] = f"{message}: {job['error']}"
        queries.finish_run(conn, run_id, status, **fields)
        conn.commit()
        log.error("run %s: %s -- %s", run_id, status,
                  fields["error_message"])
        return status

    def _status_document(self, job, run_dir):
        """The status doc from the job record, or from the mirrored file.

        Both paths matter. A job observed to completion carries the parsed
        document; a job the agent finalized from disk after being restarted
        mid-run may not, and then the file we just downloaded is the only
        copy.
        """
        doc = (job or {}).get("status")
        if isinstance(doc, dict) and doc:
            return doc

        path = artifacts.find(run_dir, "status.json")
        if path is None:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("mirrored %s is unreadable: %s", path, exc)
            return None

    def _fetch_artifacts(self, run_id, device, runner, run_dir):
        """Download this job's artifacts -- whatever its outcome.

        Fetching only on success would discard the log and status document
        for every run worth investigating.
        """
        dest = run_dir / "artifacts"
        try:
            manifest = runner.manifest(run_id)
        except AgentError as exc:
            log.warning("run %s: no manifest: %s", run_id, exc)
            return {"written": [], "skipped": [], "corrupt": [], "bytes": 0,
                    "error": str(exc)}

        entries = manifest.get("artifacts") or []
        chosen = artifacts.select(entries, device.fetch_full_logs)
        summary = artifacts.mirror(runner, run_id, chosen, dest)
        log.info("run %s: %s/%s artifact(s), %.1f MiB%s", run_id,
                 len(summary["written"]), len(entries),
                 summary["bytes"] / 1048576.0,
                 f", {len(summary['corrupt'])} checksum mismatch(es)"
                 if summary["corrupt"] else "")
        return summary

    def _recover(self, run_id, device, runner, cancel_first) -> None:
        """Leave the board usable for the next run.

        A timeout or a failed flash can leave the device sitting in EDL,
        where the next nightly cannot reach it either -- so one failure
        would quietly become every subsequent failure.
        """
        if cancel_first:
            try:
                runner.cancel(run_id)
            except AgentError as exc:
                log.debug("run %s: cancel before recover failed: %s",
                          run_id, exc)
        try:
            response = runner.recover(device.device_id)
        except AgentError as exc:
            log.error("run %s: recovery failed for %s: %s -- the board may "
                      "need a human", run_id, device.device_id, exc)
            return
        log.info("run %s: recovery on %s: %s", run_id, device.device_id,
                 "ok" if response.get("recovered") else response)


def run_worker(settings, inventory, *, max_runs=None, runner_factory=None,
               stop=None) -> int:
    worker = Worker(settings, inventory, runner_factory=runner_factory)
    return worker.serve_forever(max_runs=max_runs, stop=stop)
