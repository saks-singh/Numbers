"""The worker loop: claim, submit, poll, finalize, unlock.

Driven entirely through stubs -- no Postgres, no agent, no sleeping. The
`sleep` and `now` injection points on `Worker` exist for exactly this: a
real timeout test would otherwise take an hour and forty minutes.

The properties worth a test, in rough order of what they cost if broken:

  * **the device lock is always released.** `pool._healthy()` rolls back on
    checkout and connections are cached, and a session-level advisory lock
    survives a rollback -- so a missing `unlock_device` wedges the board
    until the worker process dies. Asserted on every path, including the
    ones that raise.
  * **a re-submitted job is adopted, not duplicated.** This is the property
    that makes a coordinator-side retry safe at all.
  * **artifacts are fetched even when the job failed.** A flash failure's
    log and status document are the whole evidence base at 9 a.m.
  * **an unreachable agent is not an emergency mid-job.** The job is a child
    of the agent, not of this connection; the worker backs off and keeps
    polling, and only a run still unreachable at the deadline becomes
    `unreachable` and blocks the device.
  * **a timeout recovers the board.** A device left in EDL is a device the
    next nightly cannot reach either, so one failure would quietly become
    every subsequent failure.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from src.inventory import Device, Inventory  # noqa: E402
from src.jobs import worker as worker_mod  # noqa: E402
from src.runner.agent_client import AgentError, AgentUnreachable  # noqa: E402


# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------

class Settings:
    def __init__(self, root):
        self.artifact_root = str(root)
        self.runner_backend = "agent"
        self.poll_interval = 5
        self.claim_interval = 10
        self.agent_backoff_max = 120

    @property
    def artifact_path(self):
        return Path(self.artifact_root)

    def run_dir(self, run_id):
        return self.artifact_path / "runs" / str(run_id)


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    """`pool.connection()` as a context manager over one shared FakeConn."""

    DatabaseError = RuntimeError

    def __init__(self):
        self.conn = FakeConn()

    def connection(self):
        pool = self

        class _Ctx:
            def __enter__(self):
                return pool.conn

            def __exit__(self, *exc):
                return False

        return _Ctx()


class FakeQueries:
    def __init__(self, queue=(), lockable=True):
        self.queue = list(queue)
        self.lockable = lockable
        self.locked = []
        self.unlocked = []
        self.fields = {}
        self.finished = []
        self.details = {}

    def claim_next_run(self, conn, exclude_devices=None):
        exclude = set(exclude_devices or ())
        for index, run in enumerate(self.queue):
            if run["device_id"] not in exclude:
                return self.queue.pop(index)
        return None

    def try_lock_device(self, conn, device_id):
        self.locked.append(device_id)
        return self.lockable

    def unlock_device(self, conn, device_id):
        self.unlocked.append(device_id)

    def set_run_fields(self, conn, run_id, **fields):
        self.fields.setdefault(run_id, {}).update(fields)

    def finish_run(self, conn, run_id, status, **fields):
        self.finished.append((run_id, status, fields))

    def run_detail(self, conn, run_id):
        return self.details.get(run_id, {})


class FakeIngest:
    class IngestError(Exception):
        pass

    def __init__(self, result=None, error=None):
        self.result = result or {"status": "success", "recorded": 6,
                                 "boots": 6, "parse_errors": []}
        self.error = error
        self.calls = []

    def ingest_status(self, conn, run_id, doc, **kw):
        self.calls.append((run_id, doc, kw))
        if self.error:
            raise self.IngestError(self.error)
        return self.result

    @staticmethod
    def _utc(value):
        return None


STATUS_OK = {
    "schema_version": 1, "exit_code": 0, "failure_stage": None,
    "boots_expected": 6, "boots_recorded": 6, "partial": False,
    "stages_run": ["flash", "capture", "report"],
    "ended_utc": "2026-10-06T01:46:11+00:00",
}


class StubRunner:
    """The Runner protocol, verb for verb, with scripted answers."""

    def __init__(self, *, states=("running", "done"), status=None,
                 manifest=None, blobs=None, submit_created=True,
                 submit_error=None, poll_errors=(), log_chunks=None):
        self._states = list(states)
        self._status = status if status is not None else dict(STATUS_OK)
        self._manifest = manifest
        self._blobs = dict(blobs or {})
        self._submit_created = submit_created
        self._submit_error = submit_error
        self._poll_errors = list(poll_errors)
        self._log_chunks = list(log_chunks or [])
        self.calls = []

    # -- protocol --------------------------------------------------------
    def healthz(self):
        self.calls.append("healthz")
        return {"session_interactive": True, "tac_device_count": 1}

    def latest_build(self, device_id):
        self.calls.append("latest_build")
        return {"build": {"build_number": 2471}}

    def submit_job(self, job_id, device_id, **request):
        self.calls.append(("submit", job_id, request))
        if self._submit_error:
            raise self._submit_error
        return {"created": self._submit_created,
                "job": {"job_id": str(job_id), "state": "running"}}

    def get_job(self, job_id):
        self.calls.append("get_job")
        if self._poll_errors:
            raise self._poll_errors.pop(0)
        state = (self._states.pop(0) if len(self._states) > 1
                 else self._states[0])
        job = {"job_id": str(job_id), "state": state}
        if state in ("done", "failed"):
            job["exit_code"] = 0 if state == "done" else 12
            if self._status is not False:
                job["status"] = self._status
        return {"job": job}

    def tail_log(self, job_id, offset=0):
        self.calls.append(("tail", offset))
        if self._log_chunks:
            text = self._log_chunks.pop(0)
            return {"offset": offset, "chunk": text,
                    "size": offset + len(text), "eof": False}
        return {"offset": offset, "chunk": "", "size": offset, "eof": True}

    def manifest(self, job_id):
        self.calls.append("manifest")
        if self._manifest is None:
            raise AgentError("no manifest", status=404)
        return {"artifacts": self._manifest}

    def fetch_artifact(self, job_id, relpath):
        self.calls.append(("fetch", relpath))
        return self._blobs[relpath]

    def cancel(self, job_id):
        self.calls.append("cancel")
        return {"cancelled": True}

    def recover(self, device_id):
        self.calls.append("recover")
        return {"recovered": True}


def device(**kw):
    base = dict(device_id="dev-01", target="iq-9075-evk",
                agent_url="http://bench-win-01:8765", com_port="COM7",
                tac_port="VTP8", adb_serial="1a2b3c", num_boots=3,
                boot_timeout=480)
    base.update(kw)
    return Device(**base)


def queued(run_id=42, **kw):
    base = {"run_id": run_id, "device_id": "dev-01", "status": "running",
            "build_number": 2471, "stages_requested": ["flash", "capture",
                                                       "report"],
            "trigger_source": "cron", "readopted": False}
    base.update(kw)
    return base


def manifest_entry(relpath, data):
    import hashlib
    return {"relpath": relpath, "size": len(data), "kind": "status"
            if relpath.endswith("status.json") else "runner_log",
            "sha256": hashlib.sha256(data).hexdigest()}


def inventory_of(devices):
    return Inventory({d.device_id: d for d in devices})


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bootbench-worker-"))
        self.settings = Settings(self.root)
        self.pool = FakePool()
        self.slept = []

    def build(self, *, runs=(), devices=None, runner=None, queries=None,
              ingest=None, lockable=True, clock=None):
        self.q = queries or FakeQueries(queue=runs, lockable=lockable)
        self.ingest = ingest or FakeIngest()
        self.runner = runner or StubRunner()
        inventory = inventory_of(devices if devices is not None else [device()])

        ticks = iter(clock or [])

        def now():
            try:
                return next(ticks)
            except StopIteration:
                return 0.0

        self.worker = worker_mod.Worker(
            self.settings, inventory,
            runner_factory=lambda d: self.runner,
            sleep=self.slept.append,
            now=now if clock else (lambda: 0.0))
        return self.worker

    def run_once(self):
        with patch.object(worker_mod, "pool", self.pool), \
             patch.object(worker_mod, "queries", self.q), \
             patch.object(worker_mod, "ingest", self.ingest):
            return self.worker.run_once()


# ---------------------------------------------------------------------------
# claiming
# ---------------------------------------------------------------------------

class TestClaim(WorkerCase):
    def test_empty_queue_returns_none_and_touches_nothing(self):
        self.build(runs=[])
        self.assertIsNone(self.run_once())
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.q.unlocked, [])

    def test_unlockable_device_is_deferred_not_spun_on(self):
        """Another worker holds the device. The claim goes back and this
        worker looks for work elsewhere rather than retrying the same row."""
        self.build(runs=[queued(42)], lockable=False)
        self.assertIsNone(self.run_once())
        self.assertIn("dev-01", self.worker._deferred)
        self.assertEqual(self.q.locked, ["dev-01"])

    def test_device_lock_is_released_on_the_happy_path(self):
        self.build(runs=[queued(42)])
        self.run_once()
        self.assertEqual(self.q.unlocked, ["dev-01"])

    def test_device_lock_is_released_even_when_execution_raises(self):
        """The reason `unlock_device` is in a `finally`: a session-level
        advisory lock survives the rollback `pool` does on checkout, so a
        leaked lock wedges the board until this process dies."""
        self.build(runs=[queued(42)])
        with patch.object(self.worker, "_execute",
                          side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_once()
        self.assertEqual(self.q.unlocked, ["dev-01"])

    def test_run_for_an_unknown_device_is_failed_not_left_running(self):
        """The run outlived its devices.yaml entry. Leaving it `running`
        would block the device it names forever."""
        self.build(runs=[queued(42, device_id="ghost-01")],
                   devices=[device()])
        result = self.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.q.finished[0][1], "failed")
        self.assertEqual(self.q.finished[0][2]["error_class"], "ConfigError")
        self.assertEqual(self.q.unlocked, ["ghost-01"])


# ---------------------------------------------------------------------------
# submission
# ---------------------------------------------------------------------------

class TestSubmit(WorkerCase):
    def test_job_id_is_the_run_id(self):
        """One number spans the URL, the agent's job directory, and the
        status filename."""
        self.build(runs=[queued(42)])
        self.run_once()
        submits = [c for c in self.runner.calls if c[0] == "submit"]
        self.assertEqual(submits[0][1], 42)
        self.assertEqual(self.q.fields[42]["agent_job_id"], "42")

    def test_request_carries_the_device_s_own_boot_settings(self):
        self.build(runs=[queued(42)],
                   devices=[device(num_boots=1, boot_timeout=600)])
        self.run_once()
        request = [c for c in self.runner.calls if c[0] == "submit"][0][2]
        self.assertEqual(request["num_boots"], 1)
        self.assertEqual(request["boot_timeout"], 600)

    def test_resume_pull_only_for_a_retry_of_a_capture(self):
        cases = [
            (queued(42, trigger_source="retry",
                    stages_requested=["capture"]), True),
            # A hand-triggered capture wants a fresh capture, not a re-pull
            # of whatever happens to be in /data.
            (queued(43, trigger_source="manual",
                    stages_requested=["capture"]), False),
            (queued(44, trigger_source="retry",
                    stages_requested=["flash", "capture", "report"]), False),
        ]
        for run, expected in cases:
            with self.subTest(run_id=run["run_id"]):
                self.build(runs=[run])
                self.run_once()
                request = [c for c in self.runner.calls
                           if c[0] == "submit"][0][2]
                self.assertEqual(request["resume_pull"], expected)

    def test_unreachable_agent_at_submit_requeues(self):
        """Nothing started, so requeueing is safe -- and would be safe even
        if it had, because submit is idempotent on job_id."""
        self.build(runs=[queued(42)],
                   runner=StubRunner(submit_error=AgentUnreachable("refused")))
        result = self.run_once()
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.q.fields[42]["status"], "queued")
        self.assertIsNone(self.q.fields[42]["started_at"])
        self.assertEqual(self.q.finished, [])

    def test_busy_agent_requeues_and_defers_the_device(self):
        error = AgentError("busy", status=409, payload={"job_id": "41"})
        self.build(runs=[queued(42)], runner=StubRunner(submit_error=error))
        result = self.run_once()
        self.assertEqual(result["status"], "queued")
        self.assertIn("dev-01", self.worker._deferred)

    def test_refused_job_is_terminal_not_retried(self):
        """A 404 on an unknown device_id or a 400 on a malformed request is
        a config bug; 01:15 would hit it again."""
        error = AgentError("no such device", status=404)
        self.build(runs=[queued(42)], runner=StubRunner(submit_error=error))
        result = self.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.q.finished[0][1], "failed")

    def test_known_job_is_adopted_not_duplicated(self):
        """The property that makes a coordinator-side retry safe: a job the
        agent already has comes back as the existing record."""
        self.build(runs=[queued(42)], runner=StubRunner(submit_created=False))
        result = self.run_once()
        self.assertEqual(result["status"], "success")
        self.assertTrue(self.q.fields[42]["readopted"])


# ---------------------------------------------------------------------------
# polling
# ---------------------------------------------------------------------------

class TestPoll(WorkerCase):
    def test_polls_until_terminal(self):
        self.build(runs=[queued(42)],
                   runner=StubRunner(states=["running", "running", "done"]))
        result = self.run_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.runner.calls.count("get_job"), 3)
        self.assertEqual(self.slept, [5, 5])

    def test_log_is_mirrored_locally(self):
        """`/runs/<id>/log` must keep working after the agent prunes its own
        job directories."""
        self.build(runs=[queued(42)],
                   runner=StubRunner(states=["running", "done"],
                                     log_chunks=["flashing\n", "booting\n"]))
        self.run_once()
        path = self.settings.run_dir(42) / "runner.log"
        self.assertEqual(path.read_text(), "flashing\nbooting\n")

    def test_log_offset_advances_by_bytes_not_characters(self):
        """The agent reports a byte offset; treating it as a character count
        drifts on any non-ASCII line PCAT happens to print."""
        self.build(runs=[queued(42)],
                   runner=StubRunner(states=["done"], log_chunks=["café\n"]))
        self.run_once()
        tails = [c for c in self.runner.calls if c[0] == "tail"]
        self.assertEqual(tails[0][1], 0)
        # "café\n" is 6 bytes in UTF-8, not 5 characters.
        self.assertEqual(tails[1][1], 6)

    def test_transient_poll_error_is_retried(self):
        self.build(runs=[queued(42)],
                   runner=StubRunner(states=["done"],
                                     poll_errors=[AgentError("502",
                                                             status=502)]))
        result = self.run_once()
        self.assertEqual(result["status"], "success")

    def test_unreachable_agent_backs_off_exponentially(self):
        """Not an emergency: the job is a child of the agent, not of this
        connection, and it keeps running."""
        errors = [AgentUnreachable("down")] * 4
        self.build(runs=[queued(42)],
                   runner=StubRunner(states=["done"], poll_errors=errors))
        result = self.run_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.slept[:4], [5, 10, 20, 40])

    def test_backoff_is_capped(self):
        errors = [AgentUnreachable("down")] * 8
        self.build(runs=[queued(42)],
                   runner=StubRunner(states=["done"], poll_errors=errors))
        self.run_once()
        self.assertLessEqual(max(self.slept), self.settings.agent_backoff_max)

    def test_cancel_is_forwarded_once(self):
        self.q = FakeQueries(queue=[queued(42)])
        self.q.details[42] = {"cancel_requested": True}
        self.build(runs=[], queries=self.q,
                   runner=StubRunner(states=["running", "running", "done"]))
        self.q.queue = [queued(42)]
        self.run_once()
        self.assertEqual(self.runner.calls.count("cancel"), 1)

    def test_cancel_not_forwarded_when_not_requested(self):
        self.build(runs=[queued(42)])
        self.run_once()
        self.assertEqual(self.runner.calls.count("cancel"), 0)


# ---------------------------------------------------------------------------
# timeout / unreachable
# ---------------------------------------------------------------------------

class TestTimeout(WorkerCase):
    def test_timeout_marks_the_run_and_recovers_the_board(self):
        """A board left in EDL is a board the next nightly cannot reach
        either, so one failure would become every subsequent failure."""
        # now(): the deadline is computed from the first call, and the second
        # is already past it.
        self.build(runs=[queued(42)], clock=[0.0, 10 ** 9, 10 ** 9, 10 ** 9],
                   runner=StubRunner(states=["running"]))
        result = self.run_once()
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(self.q.finished[0][1], "timeout")
        self.assertIn("cancel", self.runner.calls)
        self.assertIn("recover", self.runner.calls)

    def test_persistently_unreachable_run_blocks_the_device(self):
        """A run nobody can account for must not silently become the
        baseline for the next comparison."""
        self.build(runs=[queued(42)], clock=[0.0, 10 ** 9, 10 ** 9, 10 ** 9],
                   runner=StubRunner(states=["running"],
                                     poll_errors=[AgentUnreachable("gone")]))
        result = self.run_once()
        self.assertEqual(result["status"], "unreachable")
        self.assertEqual(self.q.finished[0][1], "unreachable")
        # Deliberately no recovery: we have no idea what the board is doing.
        self.assertNotIn("recover", self.runner.calls)

    def test_timeout_budget_is_generous(self):
        """A timeout should mean "wedged", not "slow night"."""
        from src.runner.base import job_timeout_seconds
        self.assertGreater(job_timeout_seconds(3, 480), 3600)


# ---------------------------------------------------------------------------
# finalization
# ---------------------------------------------------------------------------

class TestFinalize(WorkerCase):
    def test_status_document_from_the_job_record_is_ingested(self):
        self.build(runs=[queued(42)])
        result = self.run_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.ingest.calls), 1)
        self.assertEqual(self.ingest.calls[0][1]["schema_version"], 1)
        self.assertEqual(self.q.finished[0][1], "success")

    def test_status_document_recovered_from_the_mirrored_file(self):
        """A job the agent finalized from disk after being restarted mid-run
        may not carry the parsed document, and then the file we just
        downloaded is the only copy."""
        blob = json.dumps(STATUS_OK).encode()
        self.build(runs=[queued(42)],
                   runner=StubRunner(status=False,
                                     manifest=[manifest_entry("status.json",
                                                              blob)],
                                     blobs={"status.json": blob}))
        result = self.run_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.ingest.calls[0][1]["schema_version"], 1)

    def test_no_status_document_anywhere_is_a_recorded_failure(self):
        self.build(runs=[queued(42)], runner=StubRunner(status=False))
        result = self.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.q.finished[0][2]["error_class"],
                         "NoStatusDocument")

    def test_agent_exit_code_is_recorded_even_with_no_status(self):
        """At 9 a.m. the agent's job record is the whole evidence base."""
        self.build(runs=[queued(42)],
                   runner=StubRunner(states=["failed"], status=False))
        self.run_once()
        self.assertEqual(self.q.finished[0][2]["exit_code"], 12)

    def test_ingest_failure_does_not_leave_the_run_running(self):
        """The run happened; we just cannot model it. Recording that plainly
        beats a row stuck in `running` forever."""
        self.build(runs=[queued(42)],
                   ingest=FakeIngest(error="boot 3 has no log_name"))
        result = self.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.q.finished[0][2]["error_class"], "IngestError")
        self.assertEqual(self.pool.conn.rollbacks, 1)

    def test_partial_is_whatever_ingest_decided(self):
        """Exit 0 does not mean success: 5 boots out of 6 is `partial`, and
        that judgement lives in one place."""
        self.build(runs=[queued(42)],
                   ingest=FakeIngest({"status": "partial", "recorded": 5,
                                      "boots": 6,
                                      "parse_errors": ["Logs-default-2"]}))
        result = self.run_once()
        self.assertEqual(result["status"], "partial")

    def test_failed_flash_recovers_the_board(self):
        doc = dict(STATUS_OK, exit_code=12, failure_stage="flash")
        self.build(runs=[queued(42)], runner=StubRunner(status=doc),
                   ingest=FakeIngest({"status": "failed", "recorded": 0,
                                      "boots": 6, "parse_errors": []}))
        self.run_once()
        self.assertIn("recover", self.runner.calls)

    def test_failed_parse_does_not_recover(self):
        """The board is fine; it booted. Power-cycling it into EDL would be
        a worse state than the one we are in."""
        doc = dict(STATUS_OK, exit_code=16, failure_stage="parse")
        self.build(runs=[queued(42)], runner=StubRunner(status=doc),
                   ingest=FakeIngest({"status": "failed", "recorded": 0,
                                      "boots": 6, "parse_errors": ["x"]}))
        self.run_once()
        self.assertNotIn("recover", self.runner.calls)

    def test_lost_job_is_distinguished_from_a_crash(self):
        self.build(runs=[queued(42)],
                   runner=StubRunner(status=False,
                                     poll_errors=[AgentError("gone",
                                                             status=404)]))
        result = self.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertIn("pruned", self.q.finished[0][2]["error_message"])


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------

class TestArtifacts(WorkerCase):
    def test_artifacts_are_fetched_even_when_the_job_failed(self):
        """The headline rule: a flash failure's runner.log and status
        document are exactly what you want at 9 a.m."""
        log_blob = b"flashing\nPCAT failed\n"
        doc = dict(STATUS_OK, exit_code=12, failure_stage="flash")
        blob = json.dumps(doc).encode()
        self.build(
            runs=[queued(42)],
            runner=StubRunner(states=["failed"], status=doc,
                              manifest=[manifest_entry("status.json", blob),
                                        manifest_entry("runner.log",
                                                       log_blob)],
                              blobs={"status.json": blob,
                                     "runner.log": log_blob}),
            ingest=FakeIngest({"status": "failed", "recorded": 0, "boots": 6,
                               "parse_errors": []}))
        result = self.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["artifacts"]["written"]), 2)
        dest = self.settings.run_dir(42) / "artifacts"
        self.assertTrue((dest / "status.json").is_file())

    def test_missing_manifest_is_not_fatal(self):
        self.build(runs=[queued(42)])
        result = self.run_once()
        self.assertEqual(result["status"], "success")
        self.assertIn("error", result["artifacts"])

    def test_corrupt_artifact_does_not_cost_the_run(self):
        good = json.dumps(STATUS_OK).encode()
        entry = manifest_entry("runner.log", b"real bytes")
        self.build(runs=[queued(42)],
                   runner=StubRunner(manifest=[manifest_entry("status.json",
                                                              good), entry],
                                     blobs={"status.json": good,
                                            "runner.log": b"tampered"}))
        result = self.run_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["artifacts"]["corrupt"], ["runner.log"])


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

class TestServeForever(WorkerCase):
    def serve(self, **kw):
        with patch.object(worker_mod, "pool", self.pool), \
             patch.object(worker_mod, "queries", self.q), \
             patch.object(worker_mod, "ingest", self.ingest):
            return self.worker.serve_forever(**kw)

    def test_max_runs_stops_the_loop(self):
        self.build(runs=[queued(42), queued(43)])
        self.assertEqual(self.serve(max_runs=1), 1)

    def test_stop_callback_stops_the_loop(self):
        self.build(runs=[])
        self.assertEqual(self.serve(stop=lambda: True), 0)

    def test_empty_queue_sleeps_the_claim_interval(self):
        self.build(runs=[])
        calls = iter([False, False, True])
        self.serve(stop=lambda: next(calls))
        self.assertEqual(self.slept, [10, 10])

    def test_deferred_devices_are_cleared_when_the_queue_empties(self):
        """A permanently excluded device looks exactly like a stuck queue."""
        self.build(runs=[], lockable=False)
        self.worker._deferred.add("dev-01")
        calls = iter([False, True])
        self.serve(stop=lambda: next(calls))
        self.assertEqual(self.worker._deferred, set())

    def test_database_outage_backs_off_instead_of_exiting(self):
        """systemd would restart us into the same condition at a faster rate
        than this backoff."""
        self.build(runs=[])
        with patch.object(self.worker, "run_once",
                          side_effect=FakePool.DatabaseError("no server")):
            calls = iter([False, True])
            self.serve(stop=lambda: next(calls))
        self.assertEqual(self.slept, [10])

    def test_heartbeat_is_written_for_the_web_app(self):
        self.build(runs=[])
        path = self.worker.heartbeat()
        self.assertEqual(path.name, worker_mod.HEARTBEAT_NAME)
        self.assertTrue(path.is_file())
        self.assertIn("T", path.read_text())

    def test_local_fake_backend_without_a_factory_is_refused(self):
        """A coordinator configured that way would report healthy nightlies
        while benchmarking nothing."""
        self.settings.runner_backend = "local_fake"
        bare = worker_mod.Worker(self.settings, inventory_of([device()]))
        with self.assertRaises(RuntimeError):
            bare.runner_for(device())

    def test_runners_are_cached_per_agent_url(self):
        made = []

        def factory(dev):
            made.append(dev.agent_url)
            return StubRunner()

        inventory = inventory_of([device(device_id="dev-01"),
                                  device(device_id="dev-02",
                                         adb_serial="9z8y7x")])
        w = worker_mod.Worker(self.settings, inventory,
                              runner_factory=factory, sleep=lambda s: None)
        w.runner_for(inventory["dev-01"])
        w.runner_for(inventory["dev-02"])
        self.assertEqual(len(made), 1)


if __name__ == "__main__":
    unittest.main()
