#!/usr/bin/env python3
"""
bootbench agent -- runs on the Windows bench host, owns the hardware.

The Linux coordinator POSTs a job here; this process runs bootbench.py
locally and exposes its status, logs, and artifacts over HTTP. The
coordinator never learns a C: path and never opens a shell on this box --
it sends intent ("run 'all' on iq-9075-evk-01 with 3 boots") and this
agent composes the argv from its own config.

>>> LAUNCH THIS FROM A LOGGED-ON DESKTOP SESSION. NOT AS A SERVICE. <<<

    The Alpaca TAC is driven through an out-of-process COM server
    (TACCOM.AlpacaTACServer). COM servers like that routinely refuse to
    start -- or start in Session 0 with no USB/HID access -- when the
    caller has no interactive window station. A Windows Service runs in
    Session 0. An agent started from shell:startup runs in the logged-on
    session, which is exactly where the TAC already works when an
    engineer runs the skill by hand.

    Converting this to a Service will break every flash with an opaque
    COM error. GET /healthz reports `session_id` and `tac_device_count`
    precisely so that regression is visible the moment it happens.

    See install/README.md.

Dependencies: Python standard library only, plus the two packages the
bootbench skill itself already needs on this host (comtypes, pyserial).
The TAC probe degrades to `null` if comtypes is missing rather than
refusing to start, so the agent still comes up on a box being set up.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AGENT_VERSION = "1.0.0"
STATUS_SCHEMA_MIN = 1

# Files read_pulled_logs() needs to parse one boot, plus kernel_cmdline.txt
# (which the collect script already writes). Everything else a boot folder
# contains -- plot_systemd.svg, proc_config.txt, Boot_Trace.txt, lsmod.txt --
# is classified `boot_extra` and only shipped when the coordinator asks.
PARSE_FILES = frozenset({
    "systemd_analyze.txt",
    "systemd-analyze_critical_chain.txt",
    "systemd-analyze_critical_chain_sysinit.txt",
    "blame_systemd.txt",
    "dmesg.txt",
    "journalctl.log",
    "kernel_cmdline.txt",
})

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

TERMINAL_STATES = frozenset({"done", "failed", "orphaned"})


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[{_utc()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_PCAT = r"C:\Program Files (x86)\Qualcomm\PCAT\bin\PCAT.exe"


class ConfigError(Exception):
    pass


class AgentConfig:
    """Parsed agentconfig.json. Everything the coordinator must *not* know
    about -- interpreter, script path, share root, where artifacts live --
    lives here, because it describes this machine's filesystem."""

    def __init__(self, raw: dict, source: Path):
        self.source = source
        self.bind = raw.get("bind", "127.0.0.1")
        self.port = int(raw.get("port", 8765))
        self.token_env = raw.get("token_env", "BOOTBENCH_AGENT_TOKEN")
        self.python = raw.get("python", "py -3")
        self.pcat_exe = raw.get("pcat_exe", DEFAULT_PCAT)
        self.keep_jobs = int(raw.get("keep_jobs", 200))

        bootbench = raw.get("bootbench")
        if not bootbench:
            raise ConfigError("'bootbench' (path to bootbench.py) is required")
        self.bootbench = Path(bootbench)

        share_root = raw.get("share_root")
        if not share_root:
            raise ConfigError("'share_root' is required")
        self.share_root = share_root

        artifact_root = raw.get("artifact_root")
        if not artifact_root:
            raise ConfigError("'artifact_root' is required")
        self.artifact_root = Path(artifact_root)

        # Nightly multi-MB log trees in a synced folder cause sync storms, and
        # OneDrive's file locking makes `adb pull` fail mid-write in a way that
        # looks like an adb bug. Refuse at startup rather than at 1 a.m.
        if "onedrive" in str(self.artifact_root).lower():
            raise ConfigError(
                f"artifact_root must not be inside OneDrive (got {self.artifact_root}). "
                "Sync storms and file locking will corrupt adb pulls; use a local path "
                "such as C:/bench/artifacts."
            )

        devices = raw.get("devices") or {}
        if not devices:
            raise ConfigError("'devices' must contain at least one device")
        self.devices: dict[str, dict] = {}
        for device_id, dev in devices.items():
            if not DEVICE_ID_RE.match(device_id):
                raise ConfigError(
                    f"device id {device_id!r} must match {DEVICE_ID_RE.pattern}"
                )
            if not dev.get("target"):
                raise ConfigError(f"device {device_id!r} is missing 'target'")
            self.devices[device_id] = dev

        # Every config value below becomes an argv element. There is no shell,
        # so metacharacters are harmless -- but a stray newline or NUL would
        # corrupt the argv itself, and that is always a config bug.
        for key, value in self._argv_values():
            if any(c in value for c in "\r\n\x00"):
                raise ConfigError(f"{key} contains a control character: {value!r}")

    def _argv_values(self):
        yield "python", self.python
        yield "bootbench", str(self.bootbench)
        yield "share_root", self.share_root
        yield "artifact_root", str(self.artifact_root)
        for device_id, dev in self.devices.items():
            for field in ("target", "com_port", "tac_port", "adb_serial"):
                if dev.get(field):
                    yield f"devices.{device_id}.{field}", str(dev[field])

    @classmethod
    def load(cls, path: Path) -> AgentConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"config not found: {path}")
        except json.JSONDecodeError as e:
            raise ConfigError(f"config is not valid JSON ({path}): {e}")
        return cls(raw, path)

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env) or None

    def device_dir(self, device_id: str) -> Path:
        return self.artifact_root / device_id

    def job_dir(self, device_id: str, job_id: str) -> Path:
        return self.device_dir(device_id) / "jobs" / job_id


# ---------------------------------------------------------------------------
# Host capability probes -- what /healthz reports
# ---------------------------------------------------------------------------

def _proc_start_time(pid: int) -> int | None:
    """Process creation time as a raw 64-bit FILETIME, or None if unavailable.

    Paired with the pid to identify a process across an agent restart. A pid
    alone is not an identity: Windows recycles pids, so an agent that was
    down long enough could "re-adopt" an unrelated process, wait on it
    forever, and hold the device busy with a job that is actually dead.
    """
    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            # Linux: field 22 of /proc/<pid>/stat, in clock ticks since boot.
            # Only used by the test suite; the real agent is Windows-only.
            with open(f"/proc/{pid}/stat", "rb") as fh:
                return int(fh.read().rsplit(b")", 1)[1].split()[19])
        except (OSError, IndexError, ValueError):
            return None

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
    )
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        other = (wintypes.FILETIME * 3)()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(created),
            ctypes.byref(other[0]), ctypes.byref(other[1]), ctypes.byref(other[2]),
        )
        if not ok:
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    """True if pid is still running. Used at startup to decide whether a job
    left `running` by a previous agent process is still in flight."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, wintypes.DWORD(pid))
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _is_same_process(pid: int, start_time: int | None) -> bool:
    """True if `pid` is alive AND is the same process we started.

    If we never recorded a start time (a job.json from an older agent), fall
    back to liveness alone -- the pid-reuse window is narrow, and refusing to
    adopt a genuinely running flash would be the worse error.
    """
    if not _pid_alive(pid):
        return False
    if start_time is None:
        return True
    current = _proc_start_time(pid)
    return current is None or current == start_time


def _session_id() -> int | None:
    """Windows terminal-services session for this process. 0 means no
    interactive window station, which means the TAC COM server will not
    work. Anything >= 1 is a logged-on session. None off Windows."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    sid = wintypes.DWORD()
    ok = ctypes.windll.kernel32.ProcessIdToSessionId(
        wintypes.DWORD(os.getpid()), ctypes.byref(sid)
    )
    return int(sid.value) if ok else None


def _tac_probe() -> dict:
    """Live Get_Device_Count() against the Alpaca TAC COM server.

    This is the single most important thing the agent reports: if the count
    is >= 1, out-of-process COM works from this session and a flash can
    power-cycle the board. A null count carries `tac_error` saying why, so
    "comtypes isn't installed" is never mistaken for "the session is wrong".

    Read-only on purpose: Get_Device_Count and Get_PortName do not claim the
    device, so this is safe to poll. We never call OpenByName here.
    """
    try:
        import comtypes
        import comtypes.client as cc
    except ImportError:
        return {
            "tac_device_count": None,
            "tac_error": "comtypes is not installed (py -3 -m pip install comtypes)",
            "tac_ports": [],
        }
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        # `server` must stay referenced for as long as `tac` is used:
        # Create_TAC_Server() hands back a *child* interface, and letting the
        # parent be collected disconnects it mid-call. The skill works around
        # this with a module-level ref because it returns the child; a local
        # suffices here because we finish with `tac` before returning.
        server = cc.CreateObject("TACCOM.AlpacaTACServer")
        tac = server.Create_TAC_Server()
        count = int(tac.Get_Device_Count())
        ports = []
        for index in range(count):
            try:
                ports.append(str(tac.Get_PortName(index)))
            except Exception:
                pass
        return {"tac_device_count": count, "tac_error": None, "tac_ports": ports}
    except Exception as e:
        return {
            "tac_device_count": None,
            "tac_error": f"{type(e).__name__}: {e}",
            "tac_ports": [],
        }


def _adb_devices() -> list:
    try:
        out = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _free_gb(path: Path) -> float | None:
    """Free space on the volume holding `path`, walking up to the first
    parent that exists -- disk_usage raises on a path that has not been
    created yet, and 'unknown free space' would be a misleading answer when
    the volume is perfectly readable."""
    for candidate in (path, *path.parents):
        try:
            return round(shutil.disk_usage(candidate).free / 1e9, 1)
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class Job:
    """One bootbench.py invocation and everything we know about it.

    Persisted to <job_dir>/job.json on every state change so a restarted
    agent can re-adopt or finalize it.
    """

    def __init__(self, job_id: str, device_id: str, request: dict, job_dir: Path):
        self.job_id = job_id
        self.device_id = device_id
        self.request = request
        self.job_dir = job_dir
        self.state = "starting"       # starting|running|done|failed|orphaned
        self.pid = None
        self.pid_start = None         # FILETIME; guards against pid reuse
        self.argv = []
        self.exit_code = None
        self.started_utc = _utc()
        self.ended_utc = None
        self.cancel_requested = False
        self.cancel_refused_reason = None
        self.error = None
        self.manifest = None
        self._lock = threading.RLock()

    # -- paths ----------------------------------------------------------
    @property
    def log_path(self) -> Path:
        return self.job_dir / "runner.log"

    @property
    def status_path(self) -> Path:
        return self.job_dir / "status.json"

    @property
    def cancel_path(self) -> Path:
        return self.job_dir / "cancel"

    @property
    def record_path(self) -> Path:
        return self.job_dir / "job.json"

    # -- persistence ----------------------------------------------------
    def to_record(self) -> dict:
        return {
            "job_id": self.job_id,
            "device_id": self.device_id,
            "request": self.request,
            "state": self.state,
            "pid": self.pid,
            "pid_start": self.pid_start,
            "argv": self.argv,
            "exit_code": self.exit_code,
            "started_utc": self.started_utc,
            "ended_utc": self.ended_utc,
            "cancel_requested": self.cancel_requested,
            "cancel_refused_reason": self.cancel_refused_reason,
            "error": self.error,
        }

    def save(self) -> None:
        with self._lock:
            self.job_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.record_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.to_record(), indent=2), encoding="utf-8")
            os.replace(tmp, self.record_path)

    @classmethod
    def from_record(cls, record: dict, job_dir: Path) -> Job:
        job = cls(record["job_id"], record["device_id"], record.get("request", {}), job_dir)
        job.state = record.get("state", "orphaned")
        job.pid = record.get("pid")
        job.pid_start = record.get("pid_start")
        job.argv = record.get("argv", [])
        job.exit_code = record.get("exit_code")
        job.started_utc = record.get("started_utc") or job.started_utc
        job.ended_utc = record.get("ended_utc")
        job.cancel_requested = bool(record.get("cancel_requested"))
        job.cancel_refused_reason = record.get("cancel_refused_reason")
        job.error = record.get("error")
        return job

    # -- views ----------------------------------------------------------
    def status_doc(self) -> dict | None:
        """bootbench.py's own --json-status document, once it exists. This is
        what the coordinator ingests; the agent never reinterprets it."""
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def to_json(self) -> dict:
        out = self.to_record()
        out["terminal"] = self.state in TERMINAL_STATES
        out["log_size"] = self.log_path.stat().st_size if self.log_path.exists() else 0
        out["status"] = self.status_doc()
        return out


class JobStore:
    """Registry of jobs plus the per-device mutual exclusion that keeps two
    runs off one board."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self._jobs: dict[str, Job] = {}
        self._busy: dict[str, str] = {}      # device_id -> job_id
        self._lock = threading.RLock()

    # -- lookup ---------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list:
        with self._lock:
            return sorted(
                self._jobs.values(), key=lambda j: j.started_utc, reverse=True
            )

    def busy_device(self, device_id: str) -> str | None:
        with self._lock:
            return self._busy.get(device_id)

    def any_busy(self) -> str | None:
        with self._lock:
            return next(iter(self._busy.values()), None)

    # -- restart adoption ----------------------------------------------
    def adopt_existing(self) -> None:
        """Re-attach to jobs a previous agent process left behind.

        A bootbench.py child survives this agent exiting (Windows does not
        kill orphans absent a Job object), so after a restart we may find a
        flash still in progress. Three cases:

          pid alive                  -> re-adopt, resume tailing
          pid dead, status.json      -> finalize from bootbench's own doc
          pid dead, no status.json   -> orphaned (it died before its
                                        finally: block could run)

        The third case is why bootbench.py writes --json-status from a
        finally -- it makes the second case the common one.
        """
        for device_id in self.config.devices:
            jobs_root = self.config.device_dir(device_id) / "jobs"
            if not jobs_root.is_dir():
                continue
            for job_dir in sorted(jobs_root.iterdir()):
                record_path = job_dir / "job.json"
                if not record_path.is_file():
                    continue
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                job = Job.from_record(record, job_dir)
                with self._lock:
                    self._jobs[job.job_id] = job
                if job.state not in ("starting", "running"):
                    continue
                if job.pid and _is_same_process(job.pid, job.pid_start):
                    _log(f"job {job.job_id}: re-adopting live pid {job.pid}")
                    job.state = "running"
                    with self._lock:
                        self._busy[job.device_id] = job.job_id
                    threading.Thread(
                        target=self._watch_adopted, args=(job,), daemon=True
                    ).start()
                elif job.status_path.is_file():
                    doc = job.status_doc() or {}
                    job.exit_code = doc.get("exit_code")
                    job.state = "done" if job.exit_code == 0 else "failed"
                    job.ended_utc = doc.get("ended_utc") or _utc()
                    job.manifest = build_manifest(self.config, job)
                    _log(
                        f"job {job.job_id}: finalized from status.json "
                        f"(exit {job.exit_code})"
                    )
                else:
                    job.state = "orphaned"
                    job.error = (
                        "agent restarted; process is gone and bootbench.py never "
                        "wrote its status document"
                    )
                    _log(f"job {job.job_id}: orphaned")
                job.save()

    def _watch_adopted(self, job: Job) -> None:
        """Poll a re-adopted pid we did not spawn (so cannot .wait() on)."""
        while _is_same_process(job.pid, job.pid_start):
            time.sleep(5)

        doc = job.status_doc()
        if doc is None:
            # We never had a pipe to this process, so bootbench's own status
            # document is the only record of how it ended. Without it there is
            # nothing to ingest.
            state, error = "orphaned", (
                "re-adopted process exited without writing its status document"
            )
        else:
            job.exit_code = doc.get("exit_code")
            job.ended_utc = doc.get("ended_utc") or _utc()
            state = "done" if job.exit_code == 0 else "failed"
            error = None

        self._finalize(job, state, error)
        job.manifest = build_manifest(self.config, job)
        job.save()
        _log(f"job {job.job_id}: adopted process exited -> {state} "
             f"(exit {job.exit_code})")

    # -- launch ---------------------------------------------------------
    def submit(self, job_id: str, device_id: str, request: dict) -> tuple:
        """Returns (job, created). created=False means this job_id was
        already known and nothing new was started -- which is what makes a
        coordinator-side retry safe to send twice."""
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None:
                return existing, False
            busy = self._busy.get(device_id)
            if busy:
                raise Busy(busy)

            job_dir = self.config.job_dir(device_id, job_id)
            job = Job(job_id, device_id, request, job_dir)
            self._jobs[job_id] = job
            self._busy[device_id] = job_id

        job.job_dir.mkdir(parents=True, exist_ok=True)
        job.argv = build_argv(self.config, device_id, request, job)
        job.save()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job, True

    def _run(self, job: Job) -> None:
        state, error = "failed", None
        try:
            self._spawn_and_wait(job)
            state = "done" if job.exit_code == 0 else "failed"
        except Exception:
            error = traceback.format_exc(limit=4)
            _log(f"job {job.job_id}: agent-side failure\n{error}")
        finally:
            # Publish the terminal state and free the device in one step. If
            # the job went terminal before the lock was released, a
            # coordinator that polls, sees `terminal`, and immediately posts
            # the next job for this device would get a spurious 409 -- and the
            # window is as long as it takes to sha256 every artifact below.
            self._finalize(job, state, error)
            job.manifest = build_manifest(self.config, job)
            job.save()
            self._prune()

    def _finalize(self, job: Job, state: str, error: str | None = None) -> None:
        with self._lock:
            job.state = state
            job.error = error or job.error
            job.ended_utc = job.ended_utc or _utc()
            if self._busy.get(job.device_id) == job.job_id:
                del self._busy[job.device_id]
        job.save()

    def _spawn_and_wait(self, job: Job) -> None:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        _log(f"job {job.job_id}: {' '.join(job.argv)}")
        with open(job.log_path, "ab") as logfile:
            logfile.write(
                f"# bootbench agent {AGENT_VERSION} :: {_utc()}\n"
                f"# argv: {json.dumps(job.argv)}\n".encode("utf-8")
            )
            logfile.flush()
            # shell=False: argv is a list, so nothing on this host ever parses
            # a command string. No quoting rules, no %VAR% expansion, no
            # trailing-backslash hazard. stdin=DEVNULL so bootbench.py's
            # confirmation prompt cannot block (it also gets --non-interactive).
            proc = subprocess.Popen(
                job.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                cwd=str(job.job_dir),
            )
            job.pid = proc.pid
            job.pid_start = _proc_start_time(proc.pid)
            job.state = "running"
            job.save()

            assert proc.stdout is not None
            for raw in proc.stdout:
                logfile.write(raw)
                logfile.flush()          # per line, so the live tail is live
            proc.wait()

        job.exit_code = proc.returncode
        job.ended_utc = _utc()
        _log(f"job {job.job_id}: exit {proc.returncode}")

    def _prune(self) -> None:
        """Forget the oldest terminal jobs beyond keep_jobs. Only drops the
        in-memory record and the per-job dir -- Boot-Charts and Boot-Logs are
        per-device and accumulate as the skill intends."""
        with self._lock:
            terminal = [j for j in self.all() if j.state in TERMINAL_STATES]
            for job in terminal[self.config.keep_jobs:]:
                self._jobs.pop(job.job_id, None)
                shutil.rmtree(job.job_dir, ignore_errors=True)

    # -- cancel ---------------------------------------------------------
    def cancel(self, job: Job) -> dict:
        """Request cancellation. Honored only at safe points.

        bootbench.py polls the --cancel-file between boots and before the
        flash begins. We never terminate a running process: killing PCAT
        mid-write to a boot partition is not a cancellation, it is damage.
        """
        if job.state in TERMINAL_STATES:
            return {"cancelled": False, "reason": f"job is already {job.state}"}

        job.cancel_requested = True
        job.cancel_path.write_text(_utc(), encoding="utf-8")

        doc = job.status_doc() or {}
        stage = doc.get("current_stage")
        if stage == "flash":
            job.cancel_refused_reason = (
                "flash in progress -- PCAT is writing partitions; interrupting it "
                "can brick the board. Cancellation will take effect at the next "
                "safe point, or not at all for this run."
            )
        job.save()
        return {
            "cancelled": True,
            "effective": "at the next safe point (between boots, or before flashing)",
            "warning": job.cancel_refused_reason,
        }


class Busy(Exception):
    def __init__(self, job_id: str):
        super().__init__(f"device busy with job {job_id}")
        self.job_id = job_id


# ---------------------------------------------------------------------------
# argv composition
# ---------------------------------------------------------------------------

VALID_STAGES = frozenset({"flash", "capture", "report", "all", "latest-build"})


def build_argv(config: AgentConfig, device_id: str, request: dict, job: Job) -> list:
    """Compose the bootbench.py argv for a job.

    The coordinator sends intent only. Paths, interpreter, and share root
    come from this host's own config -- which is why there is nothing to
    quote and nothing to sanitize beyond the control-character check done
    at config load.
    """
    dev = config.devices[device_id]
    stages = request.get("stages", "all")
    stage_list = stages.split() if isinstance(stages, str) else list(stages)
    for stage in stage_list:
        if stage not in VALID_STAGES:
            raise ValueError(f"unknown stage {stage!r}")

    device_dir = config.device_dir(device_id)
    argv = [*shlex.split(config.python), str(config.bootbench), *stage_list]

    if stage_list == ["latest-build"]:
        argv += ["--json", "--share-root", config.share_root,
                 "--target", dev["target"]]
        return argv

    argv += ["--yes", "--non-interactive", "--target", dev["target"]]

    if dev.get("com_port"):
        argv += ["--com-port", dev["com_port"]]
    if dev.get("tac_port"):
        argv += ["--tac-port", dev["tac_port"]]
    if dev.get("adb_serial"):
        argv += ["--adb-serial", dev["adb_serial"]]

    if request.get("num_boots"):
        argv += ["--num-boots", str(int(request["num_boots"]))]
    if request.get("boot_timeout"):
        argv += ["--boot-timeout", str(int(request["boot_timeout"]))]
    if request.get("resume_pull"):
        argv += ["--resume-pull"]
    if request.get("revert_debug_cmdline"):
        argv += ["--revert-debug-cmdline"]
    if request.get("recover"):
        argv += ["--recover"]
    if request.get("build_path"):
        argv += ["--build-path", str(request["build_path"])]

    argv += [
        "--share-root", config.share_root,
        # Per-device and stable, NOT per-job: pointing --boot-charts-dir at a
        # per-job directory would give every run a fresh one-element history
        # and collapse the skill's 30-run report to a single column.
        "--boot-charts-dir", str(device_dir / "Boot-Charts"),
        "--boot-logs-dir", str(device_dir / "Boot-Logs"),
        "--json-status", str(job.status_path),
        "--cancel-file", str(job.cancel_path),
        "--lock-file", str(device_dir / ".bootbench.lock"),
    ]
    return argv


# ---------------------------------------------------------------------------
# Artifact manifest
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _classify(path: Path, inside_boot_folder: bool) -> str:
    if not inside_boot_folder:
        return "boot_txt" if path.suffix == ".txt" else "boot_extra"
    return "boot_parse" if path.name in PARSE_FILES else "boot_extra"


def build_manifest(config: AgentConfig, job: Job) -> dict:
    """Enumerate this job's artifacts with sizes and checksums.

    Boot-Charts and Boot-Logs are per-device and accumulate across runs, so
    the manifest *selects* this job's slice of them using the output paths
    bootbench.py recorded in its own status document -- rather than
    guessing, and rather than shipping every historical build's logs.
    """
    entries = []

    def add(path: Path, relpath: str, kind: str) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        entries.append({
            "relpath": relpath.replace("\\", "/"),
            "kind": kind,
            "size": stat.st_size,
            "sha256": _sha256(path),
        })

    add(job.status_path, "status.json", "status")
    add(job.log_path, "runner.log", "runner_log")

    doc = job.status_doc() or {}
    output = doc.get("output") or {}

    for key, kind in (("data_json", "chart_json"), ("html", "chart_html")):
        raw = output.get(key)
        if raw:
            path = Path(raw)
            add(path, f"Boot-Charts/{path.name}", kind)

    run_dir = output.get("boot_logs_run_dir")
    if run_dir and Path(run_dir).is_dir():
        root = Path(run_dir)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            inside = len(rel.parts) > 1 and rel.parts[0].startswith("Logs-")
            add(path, f"Boot-Logs/{rel.as_posix()}", _classify(path, inside))

    return {
        "job_id": job.job_id,
        "generated_utc": _utc(),
        "entries": entries,
        "total_bytes": sum(e["size"] for e in entries),
    }


def manifest_lookup(config: AgentConfig, job: Job, relpath: str) -> Path | None:
    """Resolve a manifest relpath to an absolute path.

    Only paths the manifest already lists are servable, which makes
    traversal (`../../`) structurally impossible rather than filtered: an
    attacker-supplied string either matches an enumerated entry or gets a
    404.
    """
    manifest = job.manifest or build_manifest(config, job)
    wanted = relpath.replace("\\", "/").lstrip("/")
    known = {e["relpath"] for e in manifest["entries"]}
    if wanted not in known:
        return None

    doc = job.status_doc() or {}
    output = doc.get("output") or {}
    if wanted == "status.json":
        return job.status_path
    if wanted == "runner.log":
        return job.log_path
    if wanted.startswith("Boot-Charts/"):
        name = wanted.split("/", 1)[1]
        for key in ("data_json", "html"):
            raw = output.get(key)
            if raw and Path(raw).name == name:
                return Path(raw)
        return None
    if wanted.startswith("Boot-Logs/"):
        run_dir = output.get("boot_logs_run_dir")
        if not run_dir:
            return None
        candidate = (Path(run_dir) / wanted.split("/", 1)[1]).resolve()
        root = Path(run_dir).resolve()
        if root not in candidate.parents and candidate != root:
            return None
        return candidate
    return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class AgentHandler(BaseHTTPRequestHandler):
    server_version = f"bootbench-agent/{AGENT_VERSION}"
    protocol_version = "HTTP/1.1"

    # injected by serve()
    config: AgentConfig = None
    store: JobStore = None
    health_cache: dict = None

    # -- plumbing -------------------------------------------------------
    def log_message(self, fmt, *args):
        _log(f"{self.address_string()} {fmt % args}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, path: Path) -> None:
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def _authorize(self) -> None:
        expected = self.config.token
        if not expected:
            raise HttpError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"agent has no token; set ${self.config.token_env}",
            )
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        presented = header[len(prefix):] if header.startswith(prefix) else ""
        if not hmac.compare_digest(presented, expected):
            _log(f"REJECTED request from {self.address_string()} ({self.path})")
            raise HttpError(HTTPStatus.UNAUTHORIZED, "bad or missing bearer token")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise HttpError(HTTPStatus.BAD_REQUEST, f"body is not valid JSON: {e}")

    def _job_or_404(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise HttpError(HTTPStatus.NOT_FOUND, f"unknown job {job_id!r}")
        return job

    # -- dispatch -------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._authorize()
            self._route(method, parts, query)
        except HttpError as e:
            self._send_json(e.status, {"error": e.message, **e.extra})
        except Exception:
            tb = traceback.format_exc(limit=6)
            _log(f"unhandled error on {method} {self.path}\n{tb}")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "agent internal error", "traceback": tb},
            )

    def _route(self, method: str, parts: list, query: dict) -> None:
        if method == "GET" and parts == ["healthz"]:
            return self._send_json(HTTPStatus.OK, self._healthz())
        if method == "GET" and parts == ["latest-build"]:
            return self._send_json(HTTPStatus.OK, self._latest_build(query))
        if method == "GET" and parts == ["jobs"]:
            return self._send_json(
                HTTPStatus.OK, {"jobs": [j.to_json() for j in self.store.all()]}
            )
        if method == "POST" and parts == ["jobs"]:
            return self._post_job()
        if method == "POST" and parts == ["recover"]:
            return self._post_recover()

        if parts and parts[0] == "jobs" and len(parts) >= 2:
            job = self._job_or_404(parts[1])
            rest = parts[2:]
            if method == "GET" and not rest:
                return self._send_json(HTTPStatus.OK, job.to_json())
            if method == "GET" and rest == ["log"]:
                return self._send_json(HTTPStatus.OK, self._tail(job, query))
            if method == "GET" and rest == ["artifacts"]:
                manifest = job.manifest or build_manifest(self.config, job)
                return self._send_json(HTTPStatus.OK, manifest)
            if method == "GET" and len(rest) >= 2 and rest[0] == "artifacts":
                return self._artifact(job, "/".join(rest[1:]))
            if method == "POST" and rest == ["cancel"]:
                return self._send_json(HTTPStatus.OK, self.store.cancel(job))

        raise HttpError(HTTPStatus.NOT_FOUND, f"no route for {method} {self.path}")

    # -- handlers -------------------------------------------------------
    def _healthz(self) -> dict:
        """Liveness plus capability assertion.

        session_id and tac_device_count are the two fields that matter:
        session 0 or a null TAC count means a flash will fail, and the
        scheduler treats that as a clean skip-with-reason instead of
        discovering it halfway through a nightly.

        The TAC probe talks to COM, so it is cached and skipped entirely
        while a job is running -- health polling must never touch the TAC
        mid-flash.
        """
        busy = self.store.any_busy()
        now = time.monotonic()
        cache = self.health_cache

        if busy or (now - cache.get("at", -1e9)) < 60:
            tac = cache.get("tac", {"tac_device_count": None, "tac_error": None,
                                    "tac_ports": []})
            adb = cache.get("adb_devices", [])
        else:
            tac = _tac_probe()
            adb = _adb_devices()
            cache.update(at=now, tac=tac, adb_devices=adb)

        session = _session_id()
        free_gb = _free_gb(self.config.artifact_root)

        return {
            "agent_version": AGENT_VERSION,
            "hostname": os.environ.get("COMPUTERNAME") or os.uname().nodename,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "session_id": session,
            "session_interactive": None if session is None else session != 0,
            "pcat_present": Path(self.config.pcat_exe).is_file(),
            "tac_device_count": tac["tac_device_count"],
            "tac_error": tac["tac_error"],
            "tac_ports": tac["tac_ports"],
            "tac_probe_skipped_busy": bool(busy),
            "adb_devices": adb,
            "devices": sorted(self.config.devices),
            "busy": busy,
            "artifact_root": str(self.config.artifact_root),
            "artifact_root_free_gb": free_gb,
        }

    def _latest_build(self, query: dict) -> dict:
        """Shell bootbench.py's device-free build probe. Touches only the SMB
        share -- no serial port, no TAC, no adb -- so the coordinator can poll
        this every 15 minutes for the cost of one directory listing."""
        device_id = (query.get("device_id") or [None])[0]
        if device_id not in self.config.devices:
            raise HttpError(
                HTTPStatus.NOT_FOUND, f"this agent does not own device {device_id!r}",
                devices=sorted(self.config.devices),
            )
        dev = self.config.devices[device_id]
        argv = [
            *shlex.split(self.config.python), str(self.config.bootbench),
            "latest-build", "--json",
            "--share-root", self.config.share_root,
            "--target", dev["target"],
        ]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise HttpError(
                HTTPStatus.BAD_GATEWAY, "latest-build failed",
                exit_code=proc.returncode,
                stderr=(proc.stderr or proc.stdout)[-4000:],
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise HttpError(
                HTTPStatus.BAD_GATEWAY,
                f"latest-build did not emit JSON: {e}",
                stdout=proc.stdout[-4000:],
            )

    def _post_job(self) -> None:
        body = self._body()
        job_id = str(body.get("job_id") or "").strip()
        device_id = str(body.get("device_id") or "").strip()

        if not JOB_ID_RE.match(job_id):
            raise HttpError(
                HTTPStatus.BAD_REQUEST,
                f"job_id must match {JOB_ID_RE.pattern} (got {job_id!r})",
            )
        if device_id not in self.config.devices:
            raise HttpError(
                HTTPStatus.NOT_FOUND,
                f"this agent does not own device {device_id!r}",
                devices=sorted(self.config.devices),
            )

        try:
            job, created = self.store.submit(job_id, device_id, body)
        except Busy as e:
            raise HttpError(
                HTTPStatus.CONFLICT, f"device {device_id} is busy", job_id=e.job_id
            )
        except (ValueError, KeyError) as e:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(e))

        # Re-POSTing a known job_id returns the original record instead of
        # launching a second run, so a coordinator retry is safe to send twice.
        status = HTTPStatus.ACCEPTED if created else HTTPStatus.OK
        payload = job.to_json()
        payload["created"] = created
        self._send_json(status, payload)

    def _post_recover(self) -> None:
        body = self._body()
        device_id = str(body.get("device_id") or "").strip()
        if device_id not in self.config.devices:
            raise HttpError(
                HTTPStatus.NOT_FOUND, f"this agent does not own device {device_id!r}"
            )
        busy = self.store.busy_device(device_id)
        if busy:
            raise HttpError(
                HTTPStatus.CONFLICT,
                f"device {device_id} is busy with job {busy}; cancel it first",
                job_id=busy,
            )
        dev = self.config.devices[device_id]
        argv = [
            *shlex.split(self.config.python), str(self.config.bootbench),
            "flash", "--recover", "--yes", "--non-interactive",
            "--target", dev["target"],
        ]
        if dev.get("tac_port"):
            argv += ["--tac-port", dev["tac_port"]]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        self._send_json(HTTPStatus.OK, {
            "device_id": device_id,
            "exit_code": proc.returncode,
            "output": (proc.stdout + proc.stderr)[-8000:],
        })

    def _tail(self, job: Job, query: dict) -> dict:
        """Byte-offset log tail. The coordinator mirrors this into its own
        runner.log and the browser polls that with the same mechanism, so
        there is one implementation of 'tail' from bench host to browser."""
        try:
            offset = max(0, int((query.get("offset") or ["0"])[0]))
        except ValueError:
            raise HttpError(HTTPStatus.BAD_REQUEST, "offset must be an integer")

        if not job.log_path.exists():
            return {"offset": 0, "size": 0, "chunk": "", "eof": job.state in TERMINAL_STATES}

        size = job.log_path.stat().st_size
        offset = min(offset, size)
        limit = 256 * 1024
        with open(job.log_path, "rb") as fh:
            fh.seek(offset)
            raw = fh.read(limit)
        return {
            "offset": offset + len(raw),
            "size": size,
            # PCAT and serial echo emit non-UTF-8 bytes; never fail a tail over it.
            "chunk": raw.decode("utf-8", errors="replace"),
            "eof": job.state in TERMINAL_STATES and offset + len(raw) >= size,
        }

    def _artifact(self, job: Job, relpath: str) -> None:
        path = manifest_lookup(self.config, job, urllib.parse.unquote(relpath))
        if path is None or not path.is_file():
            raise HttpError(
                HTTPStatus.NOT_FOUND, f"{relpath!r} is not in this job's manifest"
            )
        self._send_bytes(path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def serve(config: AgentConfig) -> None:
    config.artifact_root.mkdir(parents=True, exist_ok=True)
    store = JobStore(config)
    store.adopt_existing()

    AgentHandler.config = config
    AgentHandler.store = store
    AgentHandler.health_cache = {}

    httpd = ThreadingHTTPServer((config.bind, config.port), AgentHandler)
    httpd.daemon_threads = True

    session = _session_id()
    _log(f"bootbench agent {AGENT_VERSION} on http://{config.bind}:{config.port}")
    _log(f"pid {os.getpid()}, session {session}")
    _log(f"config: {config.source}")
    _log(f"devices: {', '.join(sorted(config.devices)) or '(none)'}")
    _log(f"artifacts: {config.artifact_root}")
    if session == 0:
        _log(
            "WARNING: running in session 0 -- no interactive window station. The "
            "Alpaca TAC COM server will almost certainly fail, so every flash will "
            "fail. Launch this agent from a logged-on desktop session instead of as "
            "a Windows Service. See install/README.md."
        )
    if not config.token:
        _log(
            f"WARNING: ${config.token_env} is not set; every request will be "
            "rejected with 500 until it is."
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")
        httpd.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="agentconfig.json",
        help="Path to agentconfig.json (default: ./agentconfig.json)",
    )
    parser.add_argument("--bind", help="Override the configured bind address")
    parser.add_argument("--port", type=int, help="Override the configured port")
    parser.add_argument(
        "--check", action="store_true",
        help="Validate config, print the health probe, and exit without serving",
    )
    args = parser.parse_args()

    try:
        config = AgentConfig.load(Path(args.config).expanduser().resolve())
    except ConfigError as e:
        print(f"agent: {e}", file=sys.stderr)
        return 2

    if args.bind:
        config.bind = args.bind
    if args.port:
        config.port = args.port

    if args.check:
        session = _session_id()
        print(json.dumps({
            "config": str(config.source),
            "devices": sorted(config.devices),
            "bootbench_exists": config.bootbench.is_file(),
            "pcat_present": Path(config.pcat_exe).is_file(),
            "session_id": session,
            "session_interactive": None if session is None else session != 0,
            **_tac_probe(),
            "adb_devices": _adb_devices(),
            "token_set": bool(config.token),
        }, indent=2))
        return 0

    serve(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
