"""An in-memory Runner for hardware-free testing.

Satisfies `base.Runner` without a bench host, a board, or a subprocess.
Used by the worker and web tests so they can exercise claim -> submit ->
poll -> manifest -> ingest against a scripted job lifecycle.

This is *not* the hardware-free end-to-end test. That one runs a real
`agent/agent.py` on localhost against `tests/fixtures/stub_bootbench.py`,
so the genuine HTTP protocol is covered. The fake exists for the cheaper
case: tests that care about the coordinator's bookkeeping and would rather
not pay for a process launch.

Kept honest by construction -- it serves artifacts from a fixture tree and
computes real sha256s, so a coordinator bug that only shows up against
real bytes still shows up here.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from .agent_client import AgentError
from .artifacts import PARSE_FILES, classify  # noqa: F401 - re-exported
from .base import TERMINAL_STATES

# `classify` and `PARSE_FILES` live in artifacts.py, which is where the
# coordinator's own download path reads them, and are re-exported here
# because the fake's manifest is the thing tests check them against. They
# mirror agent.py's classifier -- duplicated rather than imported because
# `client/` must not be importable from `server/`; it is deployed standalone
# to a host that has only the skill's dependencies. tests/test_agent.py
# asserts the two copies still agree.


class LocalFakeRunner:
    """Scripted job lifecycle over a fixture artifact tree.

    `script` controls what happens: how long the job appears to run, the
    final state, and which status document comes back. Everything else is
    computed from the tree on disk, so manifests and checksums are real.
    """

    def __init__(self, fixture_root, devices=None, duration=0.0,
                 final_state="done", health=None, latest=None, hostname="fake-bench"):
        self.root = Path(fixture_root)
        self.devices = list(devices or ["iq-9075-evk-01"])
        self.duration = duration
        self.final_state = final_state
        self.hostname = hostname
        self._health = health
        self._latest = latest
        self._jobs: dict = {}
        self._busy: dict = {}
        self._lock = threading.Lock()
        # Every call is counted so tests can assert the coordinator is not
        # hammering the agent -- an accidental tight poll loop against a real
        # bench host is a real hazard.
        self.calls: dict = {}

    # -- bookkeeping ----------------------------------------------------
    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def _job(self, job_id) -> dict:
        job = self._jobs.get(str(job_id))
        if job is None:
            raise AgentError(f"no such job: {job_id}", status=404)
        self._settle(job)
        return job

    def _settle(self, job) -> None:
        """Advance a running job to terminal once its scripted duration has
        elapsed. Lazy rather than threaded so tests control the clock by
        simply not sleeping."""
        if job["state"] not in TERMINAL_STATES and time.time() >= job["_ends"]:
            with self._lock:
                job["state"] = self.final_state
                job["exit_code"] = 0 if self.final_state == "done" else 12
                job["ended_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if self._busy.get(job["device_id"]) == job["job_id"]:
                    del self._busy[job["device_id"]]

    # -- protocol -------------------------------------------------------
    def healthz(self) -> dict:
        self._count("healthz")
        if self._health is not None:
            return dict(self._health)
        return {
            "agent_version": "fake", "hostname": self.hostname,
            "python": "3.12.0", "session_id": 1, "session_interactive": True,
            "pcat_present": True, "tac_device_count": 1, "tac_error": None,
            "tac_ports": ["VTP8"], "adb_devices": ["1a2b3c4d5e"],
            "devices": list(self.devices),
            "busy": next(iter(self._busy.values()), None),
            "artifact_root_free_gb": 500.0,
        }

    def latest_build(self, device_id: str) -> dict:
        self._count("latest_build")
        self._require_device(device_id)
        if self._latest is not None:
            return dict(self._latest)
        return {
            "device_id": device_id,
            "build": {
                "share_root": "//fake/share", "build_number": 2471,
                "build_folder": "image_Nightly_Build_master_2471",
                "build_path": "//fake/share/image_Nightly_Build_master_2471",
                "performance_path":
                    "//fake/share/image_Nightly_Build_master_2471/performance",
                "target": "iq-9075-evk",
                "build_dir":
                    "//fake/share/image_Nightly_Build_master_2471/performance/x-iq-9075-evk",
                "target_image_ready": True,
            },
        }

    def _require_device(self, device_id):
        if device_id not in self.devices:
            raise AgentError(f"unknown device_id: {device_id}", status=404)

    def submit_job(self, job_id, device_id, **request) -> dict:
        self._count("submit_job")
        job_id = str(job_id)
        self._require_device(device_id)

        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None:
                # Idempotent on job_id, exactly like the real agent: a
                # coordinator retry must not start a second flash.
                return {"created": False, "job": dict(existing)}

            busy = self._busy.get(device_id)
            if busy is not None:
                raise AgentError(
                    f"device {device_id} is busy with job {busy}",
                    status=409, payload={"job_id": busy},
                )

            now = time.time()
            job = {
                "job_id": job_id, "device_id": device_id, "request": dict(request),
                "state": "running", "pid": 4242, "exit_code": None,
                "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "ended_utc": None, "cancel_requested": False,
                "cancel_refused_reason": None, "error": None,
                "_ends": now + self.duration,
            }
            self._jobs[job_id] = job
            self._busy[device_id] = job_id

        self._settle(job)
        return {"created": True, "job": dict(job)}

    def get_job(self, job_id) -> dict:
        self._count("get_job")
        job = dict(self._job(job_id))
        status_path = self.root / "status.json"
        if job["state"] in TERMINAL_STATES and status_path.is_file():
            job["status"] = json.loads(status_path.read_text(encoding="utf-8"))
        return {"job": job}

    def list_jobs(self) -> dict:
        self._count("list_jobs")
        jobs = [dict(self._job(jid)) for jid in self._jobs]
        jobs.sort(key=lambda j: j["started_utc"], reverse=True)
        return {"jobs": jobs}

    def tail_log(self, job_id, offset: int = 0) -> dict:
        self._count("tail_log")
        job = self._job(job_id)
        path = self.root / "runner.log"
        data = path.read_bytes() if path.is_file() else b""
        offset = max(0, min(int(offset), len(data)))
        chunk = data[offset:]
        return {
            "offset": offset, "size": len(data),
            "chunk": chunk.decode("utf-8", errors="replace"),
            "eof": job["state"] in TERMINAL_STATES,
        }

    def manifest(self, job_id) -> dict:
        self._count("manifest")
        self._job(job_id)
        entries = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relpath = path.relative_to(self.root).as_posix()
            entries.append({
                "relpath": relpath,
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "kind": classify(relpath),
            })
        return {"artifacts": entries}

    def fetch_artifact(self, job_id, relpath: str) -> bytes:
        self._count("fetch_artifact")
        self._job(job_id)
        allowed = {e["relpath"] for e in self.manifest(job_id)["artifacts"]}
        if relpath not in allowed:
            # Allowlist, not a filter. Same property as the real agent: a
            # traversal attempt cannot name anything the manifest did not.
            raise AgentError(f"not in manifest: {relpath}", status=404)
        return (self.root / relpath).read_bytes()

    def cancel(self, job_id) -> dict:
        self._count("cancel")
        job = self._job(job_id)
        if job["state"] in TERMINAL_STATES:
            return {"cancelled": False, "reason": "already terminal",
                    "job": dict(job)}
        job["cancel_requested"] = True
        job["_ends"] = time.time()
        self._settle(job)
        return {"cancelled": True, "job": dict(job)}

    def recover(self, device_id: str) -> dict:
        self._count("recover")
        self._require_device(device_id)
        return {"device_id": device_id, "recovered": True, "exit_code": 0}
