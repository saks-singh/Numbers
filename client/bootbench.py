#!/usr/bin/env python3
r"""
bootbench.py -- single-file merge of edl_flash.py + boot_capture.py +
bootchart_report.py.

Automates the flash -> boot -> capture -> report pipeline for a Qualcomm
target device, split into three composable stages you name explicitly on the
command line:

  flash     Find the latest nightly Yocto build, detect/confirm the target,
            enter EDL mode, and flash it via PCAT (waits for --yes or a
            manual y/N answer right before flashing).
  capture   Log in over the serial console and collect boot-time metrics
            (systemd-analyze, dmesg, journalctl, blame, critical-chain, etc.)
            over N consecutive boots with the default kernel cmdline, then
            add debug cmdline params (initcall_debug, log_buf_len=4M,
            systemd.log_level=debug) to the on-device bootloader entry and
            capture another N boots with those active, pull all 2N logs
            via adb, and record them into the per-target JSON + regenerate
            the HTML report.
  report    Standalone JSON/HTML report maintenance (render the HTML from the
            JSON, or manually append a run) -- only does anything when run
            without 'capture' in the same command; capture already records
            and renders its own data as part of collecting it.

Pass any combination, in any order: `flash`, `flash capture`, `capture`,
`capture report`, or `all` (shorthand for `flash capture report`). See
`--help` for worked examples of each.

Run with the ARM64 Python launcher on this machine (`py -3`), or with a
regular Windows Python install (`python3`):
    py -3 bootbench.py flash [--target iq-9075-evk] [--com-port COM40] [--yes]
    python3 bootbench.py all --yes

Requires: pip install comtypes pyserial  (for the ARM64 interpreter, i.e.
`py -3 -m pip install comtypes pyserial`; for a regular Windows Python
install, `python3 -m pip install comtypes pyserial`)
Requires: adb on PATH, for the capture stage's post-boot log pull.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

EPILOG = r"""
examples:
  Flash only -- waits for a manual y/N confirmation right before flashing:
      py -3 bootbench.py flash

  Flash, then capture boot-time logs (skip the y/N prompt with --yes):
      py -3 bootbench.py flash capture --yes

  Flash, capture, and report -- the full pipeline in one command:
      py -3 bootbench.py all --yes
      py -3 bootbench.py flash capture report --yes

  Capture only, on a device that's already flashed with this build:
      py -3 bootbench.py capture --build-path "\\swayam\...\performance"

  Resume a capture that flashed/booted fine but failed during the
  adb-pull/report step (no reflash, no repeated boots):
      py -3 bootbench.py capture --build-path "\\swayam\...\performance" ^
          --target iq-9075-evk --resume-pull

  Recover a device stuck in EDL/Sahara mode:
      py -3 bootbench.py flash --recover

  Re-render the HTML report from the existing JSON (no device involved):
      py -3 bootbench.py report --report-cmd render --target iq-9075-evk

  Manually append a run JSON to the report (no device involved):
      py -3 bootbench.py report --report-cmd add-run --run-json new_run.json ^
          --target iq-9075-evk
"""

# =============================================================================
# Section 0 -- automation support: exit-code taxonomy, stage tagging, and the
# machine-readable status document.
#
# None of this changes what a human sees. Every existing `raise RuntimeError`
# site is left exactly as it was and still prints the same message; the only
# difference is that a stage wrapper translates whatever escapes into a more
# specific exit code, and that an optional --json-status file records what
# happened in a form a scheduler can read without scraping stdout.
# =============================================================================

EXIT_OK = 0
EXIT_UNKNOWN = 1          # an unexpected exception -- a bug, with a traceback
EXIT_USAGE = 2            # bad arguments, or a prompt with no one to answer it
EXIT_BUILD_DISCOVERY = 10
EXIT_EDL = 11
EXIT_FLASH = 12
EXIT_SERIAL_LOGIN = 13
EXIT_BOOT_COLLECT = 14
EXIT_ADB = 15
EXIT_PARSE = 16
EXIT_RECORD = 17
EXIT_TAC = 18
EXIT_INTERRUPTED = 130    # conventional 128+SIGINT

# Only for messages; the number is the contract.
EXIT_NAMES = {
    EXIT_OK: "ok",
    EXIT_UNKNOWN: "unknown",
    EXIT_USAGE: "usage",
    EXIT_BUILD_DISCOVERY: "build_discovery",
    EXIT_EDL: "edl",
    EXIT_FLASH: "flash",
    EXIT_SERIAL_LOGIN: "serial_login",
    EXIT_BOOT_COLLECT: "boot_collect",
    EXIT_ADB: "adb",
    EXIT_PARSE: "parse",
    EXIT_RECORD: "record",
    EXIT_TAC: "tac",
    EXIT_INTERRUPTED: "interrupted",
}

STATUS_SCHEMA_VERSION = 1


class BootbenchError(RuntimeError):
    """A RuntimeError that knows which stage failed and how to exit.

    Subclasses RuntimeError deliberately: the ~30 existing `raise
    RuntimeError(...)` sites in this file stay untouched, and any existing
    `except RuntimeError` keeps catching both kinds.
    """

    def __init__(self, message, exit_code=EXIT_UNKNOWN, stage=None):
        super().__init__(message)
        self.exit_code = exit_code
        self.stage = stage


@contextlib.contextmanager
def _stage(name: str, exit_code: int):
    """Tag whatever RuntimeError escapes this block with a stage + exit code.

    Non-RuntimeError exceptions are deliberately left alone: those are bugs
    rather than operational failures, and they should surface as EXIT_UNKNOWN
    with a full traceback instead of being dressed up as a known stage.
    """
    status_stage(name)
    try:
        yield
    except BootbenchError:
        raise  # already tagged, and the inner tag is the more specific one
    except RuntimeError as e:
        raise BootbenchError(str(e), exit_code, name) from e


# --- the status document ---------------------------------------------------
# A single module-level dict, written atomically. Module-level rather than
# threaded through every function because the stages are a sequence of
# side-effecting steps on one device, and passing a recorder through
# ~15 call sites would be a much larger diff for no gain.

_STATUS = {}


def _utcnow() -> str:
    """Timezone-aware UTC, unlike build_run's naive local minute stamp.

    The run dicts keep their existing `datetime.now().strftime("%Y-%m-%d
    %H:%M")` format because that string is a display value and a .txt backup
    filename. Everything in the status document is for a machine, so it is
    unambiguous and sorts correctly across a DST boundary.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status_init(args=None) -> dict:
    _STATUS.clear()
    _STATUS.update({
        "schema_version": STATUS_SCHEMA_VERSION,
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME"),
        "argv": list(sys.argv[1:]),
        "started_utc": _utcnow(),
        "ended_utc": None,
        "stage": None,
        "stage_history": [],
        "stages_requested": [],
        "stages_run": [],
        "target": None,
        "adb_serial": None,
        "memory_type": None,
        "storage_disk": None,
        "share_root": None,
        "build_path": None,
        "build_folder": None,
        "build_number": None,
        "boots_expected": None,
        "boots_recorded": 0,
        "partial": False,
        "exit_code": None,
        "failure_stage": None,
        "error_class": None,
        "error_message": None,
        "outputs": {},
        "boots": [],
    })
    if args is not None:
        status_set(
            target=getattr(args, "target", None),
            adb_serial=getattr(args, "adb_serial", None),
        )
    return _STATUS


def status_set(**fields) -> None:
    """Record top-level fields. None values are ignored so an early call
    cannot blank a value a later one already established."""
    for key, value in fields.items():
        if value is not None:
            _STATUS[key] = value


def status_stage(name) -> None:
    """Record the stage currently executing.

    `stage_history` is deliberately separate from `stages_run`: these are
    the fine-grained failure labels (build_discovery, edl, flash, adb, ...)
    that pair with the EXIT_* codes, whereas `stages_run` holds the
    pipeline stages the user asked for (flash/capture/report). Collapsing
    them into one list would make `failure_stage` ambiguous.
    """
    _STATUS["stage"] = name
    if name and name not in _STATUS.setdefault("stage_history", []):
        _STATUS["stage_history"].append(name)


def status_add_boot(boot: dict) -> None:
    """Append one boot's record and keep the recorded/partial counters true.

    Takes the numeric view rather than the whole run dict: the status
    document is a report on the job, not a second copy of the report.
    """
    entry = {
        "phase": boot.get("phase"),
        "boot_index": boot.get("boot_index"),
        "log_name": boot.get("log_name"),
        "timestamp": boot.get("timestamp"),
        "parse_error": boot.get("parse_error"),
        "kernel_cmdline": boot.get("kernel_cmdline"),
        "cmdline_has_debug": boot.get("cmdline_has_debug"),
    }
    if not boot.get("parse_error"):
        entry["seconds"] = run_metrics_seconds(boot)
        entry["critical_chain"] = boot.get("critical_chain")
        entry["overall_overheads"] = boot.get("overall_overheads")
        hitters = {}
        for key, metric in (boot.get("metrics") or {}).items():
            if metric.get("hitters"):
                hitters[key] = metric["hitters"]
        entry["hitters"] = hitters
        _STATUS["boots_recorded"] = _STATUS.get("boots_recorded", 0) + 1

    _STATUS.setdefault("boots", []).append(entry)
    expected = _STATUS.get("boots_expected")
    if expected:
        _STATUS["partial"] = _STATUS.get("boots_recorded", 0) < expected


def status_output(**paths) -> None:
    for key, value in paths.items():
        if value is not None:
            _STATUS.setdefault("outputs", {})[key] = str(value)


def write_status(path) -> None:
    """Write the status document atomically.

    Called from a finally block, so this runs on success, on a tagged
    failure, on Ctrl-C, and on an unexpected crash. It must therefore never
    be the thing that raises: a status file is diagnostic output, and losing
    the real error because the report about it failed to write would be a bad
    trade. Any failure here is reported on stderr and swallowed.
    """
    if not path:
        return
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory as the destination so os.replace is a rename within
        # one filesystem, which is what makes it atomic. A reader therefore
        # sees either the old file or the complete new one, never a half-
        # written one -- which matters because the agent polls this file.
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(_STATUS, f, indent=2, ensure_ascii=False, default=str)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception as e:  # noqa: BLE001 -- diagnostic output, never fatal
        print(f"Warning: could not write status file {path}: {e}", file=sys.stderr)


# --- interactive prompts ---------------------------------------------------

NON_INTERACTIVE = False


def prompt_yes_no(prompt: str) -> bool:
    """Ask for confirmation, or fail cleanly when nobody can answer.

    The agent spawns this script with stdin=DEVNULL, so input() would raise
    EOFError and surface as a traceback with EXIT_UNKNOWN -- indistinguishable
    from a real bug. --non-interactive (and the isatty check, for anyone who
    pipes the script from elsewhere) turns that into a plain usage error that
    names the flag to fix it.
    """
    if NON_INTERACTIVE or not sys.stdin or not sys.stdin.isatty():
        raise BootbenchError(
            f"Refusing to prompt with no interactive stdin: {prompt.strip()!r}. "
            f"Pass --yes to confirm non-interactively.",
            EXIT_USAGE, "usage",
        )
    return input(prompt).strip().lower() == "y"


# --- cooperative cancellation ---------------------------------------------
# The agent requests a cancel by creating a file. Polled rather than
# signalled because the hazard is specific: interrupting PCAT mid-write to a
# boot partition is damage, not a cancellation. A file checked only at points
# where nothing is mid-write cannot cause that, whereas a signal arriving at
# an arbitrary instant can.

CANCEL_FILE = None


class CancelledError(BootbenchError):
    """Raised at a safe checkpoint when a cancel has been requested."""

    def __init__(self, where):
        super().__init__(
            f"Cancelled by request ({where}).", EXIT_INTERRUPTED, "cancelled"
        )


def cancel_requested() -> bool:
    return bool(CANCEL_FILE) and Path(CANCEL_FILE).exists()


def raise_if_cancelled(where: str) -> None:
    """Stop here if a cancel is pending. Call only where it is safe to stop.

    Deliberately NOT called around run_pcat_flash: see CANCEL_FILE above.
    """
    if cancel_requested():
        print(f"\nCancel requested; stopping {where}.")
        raise CancelledError(where)


# --- report-file locking ---------------------------------------------------

@contextlib.contextmanager
def report_lock(lock_path, stale_after_s: float = 6 * 3600):
    """Serialize the load/add/save/render sequence across processes.

    The agent already serializes jobs per device, so this guards exactly the
    one race the coordinator cannot see: an engineer hand-running the skill
    while a nightly is in flight. Both would otherwise read the same JSON,
    append their own run, and the second write would silently drop the
    first's.

    O_CREAT|O_EXCL rather than a lock library, to keep this stdlib-only on
    Windows. A lock whose recorded pid is gone is broken automatically --
    otherwise a crashed run would wedge every later one, which is a worse
    failure than the race this prevents.
    """
    if not lock_path:
        yield None
        return

    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        for attempt in range(2):
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(f"{os.getpid()}\n{_utcnow()}\n")
                acquired = True
                break
            except FileExistsError:
                if attempt:
                    raise RuntimeError(
                        f"Could not acquire report lock {lock_path}; it is held "
                        f"by a live process."
                    )
                if _break_stale_lock(lock_path, stale_after_s):
                    continue
                raise RuntimeError(
                    f"Report lock {lock_path} is held by a running bootbench "
                    f"process. Wait for it to finish, or remove the file if you "
                    f"are certain it is stale."
                )
        yield lock_path
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                lock_path.unlink()


def _break_stale_lock(lock_path: Path, stale_after_s: float) -> bool:
    """Remove a lock file whose owner is gone. True if it was removed."""
    try:
        text = lock_path.read_text(encoding="utf-8")
        pid = int(text.split()[0])
    except (OSError, ValueError, IndexError):
        # Unreadable or malformed: fall back to age alone. A lock we cannot
        # attribute to a process is not evidence that the process is alive.
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            return False
        if age > stale_after_s:
            print(f"Breaking unreadable report lock {lock_path} ({age:.0f}s old).")
            with contextlib.suppress(OSError):
                lock_path.unlink()
                return True
        return False

    if _pid_alive(pid):
        return False
    print(f"Breaking stale report lock {lock_path} (pid {pid} is gone).")
    with contextlib.suppress(OSError):
        lock_path.unlink()
        return True
    return False


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check.

    Returns True when it cannot tell: refusing to break a lock we are unsure
    about is the safe direction, since the cost is a clear error message
    while the cost of guessing wrong is two processes rewriting one report.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True
    return True


# =============================================================================
# Section 1 -- edl_flash: EDL flashing, Alpaca TAC power/EDL control, serial
# console login, and PCAT device discovery/flash.
# =============================================================================

YOCTO_SHARE = r"\\swayam\QLI_Builds\Yocto"
PERFORMANCE_SUBDIR = "performance"
BUILD_FOLDER_PREFIX = "qcom-multimedia-proprietary-image"
BUILD_NAME_RE = re.compile(r"_Nightly_Build_master_(\d+)$")

PCAT_EXE = r"C:\Program Files (x86)\Qualcomm\PCAT\bin\PCAT.exe"

# PCAT's -MEMORYTYPE argument. Until this script grew `--memory-type` it was
# hardcoded to UFS, which is correct for the one board it was written against
# and wrong -- destructively wrong, since it decides how PCAT addresses the
# raw partitions -- for anything else.
#
# The mapping is from the *kernel's* block-device naming, which is what lsblk
# reports over the serial console before the flash:
#
#   nvme0n1    NVMe                      -> NVME
#   mmcblk0    eMMC (and SD cards)       -> eMMC
#   sda        SCSI-attached, i.e. UFS   -> UFS
#
# Order matters: "sd" is the loosest prefix and must be tested last. "mmcblk"
# covers both eMMC and a removable SD card, which is why detection prefers the
# disk that actually holds the mounted root over a bare prefix match.
MEMORY_TYPE_DEFAULT = "UFS"
STORAGE_PREFIX_MEMORY_TYPES = (
    ("nvme", "NVME"),
    ("mmcblk", "EMMC"),
    ("sd", "UFS"),
)
MEMORY_TYPES = tuple(dict.fromkeys(t for _, t in STORAGE_PREFIX_MEMORY_TYPES))

SERIAL_BAUD = 115200
LOGIN_USER = "root"
LOGIN_PASSWORD = "oelinux123"

SERIAL_HOSTNAME_RE = re.compile(r"hostname=([^;\\]+)")
LOGIN_PROMPT_RE = re.compile(r"\S+\s+login:")
PASSWORD_PROMPT_RE = re.compile(r"[Pp]assword:\s*$", re.MULTILINE)
SHELL_PROMPT_RE = re.compile(r"[#\$]\s*$", re.MULTILINE)
DONE_MARKER_RE = re.compile(r"__DONE_(\d+)__")

# Async kernel log lines (driver warnings, timeouts, etc.) print to the serial
# console at arbitrary times, including interleaved mid-line with a login/
# password/shell prompt with no newline in between -- which breaks the `$`
# (end-of-line) anchor in the prompt regexes above. Strip them before prompt
# matching so trailing kernel noise doesn't hide a real prompt.
KERNEL_LOG_NOISE_RE = re.compile(r"\[\s*\d+\.\d+\]\s*[^\r\n]*")

# This device's shell wraps every command's output in an OSC shell-integration
# marker (ESC ] 3008;start=...;hostname=...;cwd=...ESC \) with no newline
# separating it from the actual output -- invisible when printed to a real
# terminal (an ANSI-aware terminal consumes it silently), but present verbatim
# in the raw bytes read here, so a `uname -n`/command-output capture that
# doesn't strip it gets that marker's own "hostname="/"cwd=" text spliced into
# the result (confirmed live: corrupted a build-dir Path built from `uname -n`
# output). Always stripped in run_serial_command, regardless of strip_noise --
# unlike kernel log noise, it's never legitimate command payload (e.g. dmesg
# would never emit this OSC format itself).
OSC_SEQUENCE_RE = re.compile(r"\x1b\]\d+;.*?\x1b\\", re.DOTALL)


def _strip_kernel_log_noise(text: str) -> str:
    return KERNEL_LOG_NOISE_RE.sub("", text)


def _strip_osc_sequences(text: str) -> str:
    return OSC_SEQUENCE_RE.sub("", text)

# Fallback default for open_console() when no explicit boot_timeout is given
# (e.g. --recover doesn't open a console at all). Callers that reboot the
# device first (pre-flash target detection, post-flash boot capture) pass
# the longer --boot-timeout explicitly instead of relying on this.
PRE_FLASH_LOGIN_TIMEOUT_S = 60

TAC_PORT_NAME = None  # auto-detected from the single connected TAC device if None
_tac_server_ref = None  # keeps the AlpacaTACServer COM object alive; see _open_tac()

# Routes every adb call to one board via `adb -s <serial>`. None means plain
# `adb`, which picks arbitrarily when more than one device is attached -- fine
# for a single-board bench, wrong for a shared one. Set once in _run() from
# --adb-serial; see _adb_cmd().
ADB_SERIAL = None


def _run_powershell(command: str) -> str:
    """Runs a PowerShell command and returns stdout. Raises RuntimeError with
    PowerShell's own stderr on failure -- subprocess.run(check=True)'s
    CalledProcessError alone doesn't surface stderr, so a share access/
    permissions/connectivity error (e.g. \\\\swayam not reachable from this
    machine) shows up as a bare non-zero exit code with no explanation."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"PowerShell command failed (exit {result.returncode}): {command}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def find_latest_build_name(share_root: str) -> tuple:
    """Returns (build_number, folder_name) for the newest master nightly.

    Split out of find_latest_build so the parsed build number stops being
    discarded -- automation needs it to answer "have we already benchmarked
    this build?" without re-deriving it from a path string.

    Note the sort is on the parsed integer, so master_1234 correctly beats
    master_999. A lexical sort of the folder names would not.
    """
    stdout = _run_powershell(
        f"Get-ChildItem -LiteralPath '{share_root}' -Directory "
        f"| Select-Object -ExpandProperty Name"
    )
    candidates = []
    for name in stdout.splitlines():
        name = name.strip()
        m = BUILD_NAME_RE.search(name)
        if m:
            candidates.append((int(m.group(1)), name))
    if not candidates:
        raise RuntimeError(f"No '_Nightly_Build_master_<N>' folders found under {share_root}")
    candidates.sort()
    return candidates[-1]


def find_latest_build(share_root: str) -> Path:
    """Unchanged contract: the newest build's directory. Now a thin wrapper."""
    _build_number, latest_name = find_latest_build_name(share_root)
    return Path(share_root) / latest_name


def parse_build_number(build_path) -> int:
    """Extract the nightly build number from a build path or folder name.

    Tolerates the trailing '\\performance' that cmd_flash appends, because
    the recorded run["build_path"] is the performance directory rather than
    the build folder itself. Returns None when the path does not name a
    master nightly -- BUILD_NAME_RE only matches
    '_Nightly_Build_master_<N>', so branch and release builds land here as
    None rather than as a wrong number.
    """
    if not build_path:
        return None
    for part in reversed(Path(str(build_path)).parts):
        m = BUILD_NAME_RE.search(part.strip())
        if m:
            return int(m.group(1))
    return None


def resolve_build_dir(latest_build: Path, target: str) -> Path:
    build_dir = latest_build / PERFORMANCE_SUBDIR / f"{BUILD_FOLDER_PREFIX}-{target}"
    stdout = _run_powershell(f"Test-Path -LiteralPath '{build_dir}\\rawprogram0.xml'")
    if stdout.strip() != "True":
        raise RuntimeError(
            f"Build folder does not look like a flat build (missing rawprogram0.xml): {build_dir}"
        )
    return build_dir


def discover_latest_build(share_root: str = None, target: str = None) -> dict:
    """Report the newest nightly build without touching any hardware.

    Reads the SMB share only -- no serial port, no TAC, no adb -- so a
    scheduler can poll this cheaply every few minutes to decide whether
    there is anything new worth flashing.

    When `target` is given, reports `target_image_ready` as a bool instead of
    raising. "No new build" and "a new build exists but this target's image
    hasn't landed yet" are different situations: the first means stop, the
    second means try again shortly, and collapsing them into one exception
    would turn a routine mid-publish poll into a failed run.
    """
    if share_root is None:
        share_root = YOCTO_SHARE

    build_number, build_folder = find_latest_build_name(share_root)
    build_path = Path(share_root) / build_folder
    performance_path = build_path / PERFORMANCE_SUBDIR

    info = {
        "share_root": str(share_root),
        "build_number": build_number,
        "build_folder": build_folder,
        "build_path": str(build_path),
        "performance_path": str(performance_path),
        "target": target,
        "build_dir": None,
        "target_image_ready": None,
    }
    if target:
        build_dir = performance_path / f"{BUILD_FOLDER_PREFIX}-{target}"
        info["build_dir"] = str(build_dir)
        stdout = _run_powershell(
            f"Test-Path -LiteralPath '{build_dir}\\rawprogram0.xml'"
        )
        info["target_image_ready"] = stdout.strip() == "True"
    return info


# ---------------------------------------------------------------------------
# Serial console -- login, target detection, and post-flash boot-time data
# collection (Section 3) all go over this. See open_console().
# ---------------------------------------------------------------------------

def _drain(ser, duration_s: float = 0.5) -> str:
    time.sleep(duration_s)
    data = ser.read(65536)
    return data.decode("utf-8", errors="replace")


def _probe_serial_port(port_name: str) -> str | None:
    """Returns the raw console text read from port_name, or None if the port
    couldn't be opened. Used to auto-detect which COM port is the target's own
    console -- other consoles on the board (e.g. a co-processor debug shell)
    also enumerate and may show a bare shell prompt, so a login prompt (which
    carries the target's own hostname) is required to be confident, and a bare
    shell prompt is only accepted as a fallback if no port shows a login prompt."""
    import serial

    try:
        with serial.Serial(port_name, SERIAL_BAUD, timeout=1) as ser:
            ser.reset_input_buffer()
            ser.write(b"\r\n")
            return _drain(ser, 1.0)
    except OSError:
        # Port busy (e.g. held open by another app or the TAC control channel) or
        # doesn't exist -- not a candidate.
        return None


def find_console_port(timeout_s: int = PRE_FLASH_LOGIN_TIMEOUT_S) -> str:
    """Repeatedly sweeps every enumerated COM port (each sweep costs ~1s/port,
    from _probe_serial_port's own read window) until one shows a live target
    console, preferring the strongest signal seen so far. A single sweep run
    right after a power-cycle can catch the real target console before it has
    booted far enough to print anything, while an unrelated already-logged-in
    port (e.g. a co-processor debug shell left open on this machine) answers
    immediately -- so a one-shot scan would lock onto that wrong port forever.
    Looping gives the real console the full timeout to appear and always
    prefers a hostname/login match over a bare shell-prompt fallback,
    regardless of which one is seen first."""
    from serial.tools import list_ports

    deadline = time.time() + timeout_s
    shell_prompt_fallback = None
    while True:
        candidates = [p.device for p in list_ports.comports()]
        if not candidates:
            raise RuntimeError("No serial (COM) ports found on this machine.")

        login_fallback = None
        for port_name in candidates:
            text = _probe_serial_port(port_name)
            if text is None:
                continue
            # OSC hostname marker is the strongest signal -- it names the target's
            # own hostname directly, unlike a bare login/shell prompt which other
            # consoles on the board (e.g. a co-processor debug shell) can also show.
            if SERIAL_HOSTNAME_RE.search(text):
                return port_name
            clean = _strip_kernel_log_noise(text)
            if login_fallback is None and LOGIN_PROMPT_RE.search(clean):
                login_fallback = port_name
            if shell_prompt_fallback is None and SHELL_PROMPT_RE.search(clean.strip()):
                shell_prompt_fallback = port_name

        if login_fallback:
            return login_fallback

        if time.time() >= deadline:
            if shell_prompt_fallback:
                return shell_prompt_fallback
            raise RuntimeError(
                f"No live console found on any of: {', '.join(candidates)} within {timeout_s}s. "
                "Pass --com-port explicitly, or check the device is powered on."
            )


def wait_for_login_prompt(ser, timeout_s: int) -> str:
    """Poll the console until a login: prompt appears, nudging with a
    newline periodically (first boot after flash can take minutes)."""
    deadline = time.time() + timeout_s
    buf = ""
    last_nudge = 0.0
    while time.time() < deadline:
        buf += _drain(ser, 1.0)
        clean = _strip_kernel_log_noise(buf)
        if LOGIN_PROMPT_RE.search(clean) or SHELL_PROMPT_RE.search(clean.strip()):
            return buf
        if time.time() - last_nudge > 5.0:
            ser.write(b"\r\n")
            last_nudge = time.time()
        buf = buf[-4096:]
    raise RuntimeError(
        f"No login/shell prompt seen on serial console within {timeout_s}s. "
        "Device may still be booting or stuck -- check the console manually."
    )


def login(ser, timeout_s: int):
    print(f"Waiting up to {timeout_s}s for a login/shell prompt on serial console...")
    buf = wait_for_login_prompt(ser, timeout_s)
    clean_buf = _strip_kernel_log_noise(buf)

    if SHELL_PROMPT_RE.search(clean_buf.strip()) and not LOGIN_PROMPT_RE.search(clean_buf):
        print("Already at a shell prompt (no login required).")
        return

    print(f"Login prompt seen, logging in as {LOGIN_USER}...")
    ser.write(f"{LOGIN_USER}\r\n".encode())
    time.sleep(1.0)
    resp = _drain(ser, 2.0)

    if PASSWORD_PROMPT_RE.search(_strip_kernel_log_noise(resp)):
        ser.write(f"{LOGIN_PASSWORD}\r\n".encode())
        time.sleep(1.0)
        resp = _drain(ser, 2.0)

    deadline = time.time() + 15
    while not SHELL_PROMPT_RE.search(_strip_kernel_log_noise(resp).strip()) and time.time() < deadline:
        resp += _drain(ser, 1.0)

    if not SHELL_PROMPT_RE.search(_strip_kernel_log_noise(resp).strip()):
        raise RuntimeError(f"Login did not reach a shell prompt. Last console output:\n{resp}")
    print("Logged in.")


def run_serial_command(ser, cmd: str, timeout_s: int = 30, strip_noise: bool = True) -> str:
    """Runs cmd over the serial console, using an echoed exit-code marker to
    detect completion (there's no pexpect-style framework on a raw line).
    strip_noise=False must be used for commands whose own output legitimately
    uses the same `[ddd.ddd] ...` bracket format that _strip_kernel_log_noise
    strips (e.g. `dmesg`) -- stripping would delete the actual payload, not
    just async interleaved noise."""
    marker_cmd = f"{cmd}; echo __DONE_$?__"
    # The console echoes back exactly what was written, but the terminal's own
    # line-wrapping can insert a \r\n at an arbitrary point *inside* that echo
    # -- confirmed live: `echo __DONE_$?__` echoed back as "echo _" + newline +
    # "_DONE_$?__". A single-line substring match (the old approach) silently
    # fails to recognize a wrapped echo, leaving it stuck to the front of the
    # real command output. Tolerate an optional wrap between every character.
    echo_re = re.compile(r"\r?\n?".join(re.escape(c) for c in marker_cmd))
    ser.reset_input_buffer()
    ser.write((marker_cmd + "\r\n").encode())

    deadline = time.time() + timeout_s
    buf = ""
    while time.time() < deadline:
        buf += _drain(ser, 0.5)
        m = DONE_MARKER_RE.search(buf)
        if m:
            rc = int(m.group(1))
            output = _strip_osc_sequences(buf[:m.start()])
            # Drop the echoed command text, wherever it appears in the output
            # (kernel-log noise may have interleaved before it even reached
            # the console) -- echo_re tolerates the mid-echo line wrap.
            em = echo_re.search(output)
            if em:
                output = output[em.end():]
            lines = output.splitlines()
            text = "\n".join(lines).strip()
            if strip_noise:
                text = _strip_kernel_log_noise(text).strip()
            if rc != 0:
                raise RuntimeError(f"Command failed (rc={rc}): {cmd!r}\nOutput:\n{text}")
            return text
    raise RuntimeError(f"Timed out waiting for command to complete: {cmd!r}\nPartial output:\n{buf}")


def open_console(com_port: str = None, boot_timeout: int = PRE_FLASH_LOGIN_TIMEOUT_S):
    """Opens the serial console, waits for a login/shell prompt, and logs in.
    Returns (ser, com_port_used); caller is responsible for closing ser."""
    import serial

    if com_port is None:
        com_port = find_console_port(timeout_s=boot_timeout)

    ser = serial.Serial(com_port, SERIAL_BAUD, timeout=1)
    login(ser, boot_timeout)
    return ser, com_port


def detect_target_via_serial(ser) -> str:
    target = run_serial_command(ser, "uname -n")
    if not target:
        raise RuntimeError("Could not determine target name from 'uname -n' (empty output)")
    return target


# --- storage class detection (pre-flash) -----------------------------------
# PCAT has to be told how the device stores its partitions, and getting it
# wrong is not a no-op: -MEMORYTYPE selects how the raw partition table is
# addressed. The only reliable source for the answer is the device itself,
# while it is still booted -- after EDL entry there is no shell left to ask.

def _parse_lsblk(text: str) -> list:
    """`(name, kind, mountpoint)` for each block device line lsblk printed.

    Tolerant on purpose. `lsblk -ln` prints plain columns, but the fallback
    bare `lsblk` draws a tree (`|-sda1`), inserts a MAJ:MIN column, and omits
    the trailing mountpoint entirely when nothing is mounted. So rather than
    trusting column positions: the name is the first field with its tree
    glyphs stripped, the kind is whichever field is a known lsblk TYPE, and
    the mountpoint is the last field only if it looks like an absolute path.
    """
    kinds = ("disk", "part", "loop", "rom", "lvm", "crypt", "dm")
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("NAME"):
            continue
        fields = line.split()
        name = re.sub(r"^[^0-9A-Za-z]+", "", fields[0])
        if not name:
            continue
        kind = next((f.lower() for f in fields[1:] if f.lower() in kinds), "")
        mount = fields[-1] if len(fields) > 1 and fields[-1].startswith("/") else ""
        entries.append((name, kind, mount))
    return entries


def _memory_type_for(name: str):
    """PCAT -MEMORYTYPE for a kernel block-device name, or None."""
    for prefix, memory_type in STORAGE_PREFIX_MEMORY_TYPES:
        if name.startswith(prefix):
            return memory_type
    return None


def _choose_boot_disk(entries: list) -> tuple:
    """`(disk_name, how)` for the disk the system actually booted from.

    Two strategies, and the order is the whole point. A device can have more
    than one class of storage attached at once -- a UFS board with an SD card
    in the slot reports both `sda` and `mmcblk1` -- so a bare prefix match
    would answer eMMC for a UFS device. Resolving through a mounted partition
    cannot make that mistake.

    Falls back to a prefix match only when every disk present agrees on the
    answer, so an ambiguous device is reported as ambiguous instead of
    guessed at.
    """
    disks = [name for name, kind, _ in entries if kind == "disk"]
    if not disks:
        # Some busybox builds report no TYPE column at all, so whole disks
        # have to be told from partitions by name alone. A partition is
        # always named after its parent disk plus a suffix, so a name is a
        # disk exactly when no *other* listed name is a prefix of it. Testing
        # for a trailing digit instead would be wrong: `mmcblk0` and
        # `nvme0n1` are whole disks that end in one.
        names = [name for name, _, _ in entries if _memory_type_for(name)]
        disks = [name for name in names
                 if not any(other != name and name.startswith(other)
                            for other in names)]

    # `/` first, then `/boot`. The second matters when the rootfs is mounted
    # through dm-verity or an overlay: `/` then belongs to a `dm-*` name that
    # is nobody's partition, while `/boot` is still a raw partition of the
    # disk that was actually flashed.
    for mountpoint in ("/", "/boot"):
        owners = [name for name, _, mount in entries if mount == mountpoint]
        for owner in owners:
            for disk in sorted(disks, key=len, reverse=True):
                if owner.startswith(disk):
                    return disk, f"partition {owner} is mounted at {mountpoint}"

    candidates = {}
    for disk in disks:
        memory_type = _memory_type_for(disk)
        if memory_type:
            candidates.setdefault(memory_type, disk)
    if len(candidates) == 1:
        memory_type, disk = next(iter(candidates.items()))
        return disk, f"{disk} is the only {memory_type} disk present"
    if not candidates:
        raise RuntimeError(
            "lsblk reported no recognisable storage device. Expected a name "
            f"starting with one of {[p for p, _ in STORAGE_PREFIX_MEMORY_TYPES]}; "
            f"got {disks or 'nothing'}. Pass --memory-type to flash anyway.")
    raise RuntimeError(
        "Cannot tell which storage this device boots from: lsblk reports "
        f"{sorted(candidates.values())} ({'/'.join(sorted(candidates))}) and "
        "no partition of one of them is mounted at / or /boot to break the "
        f"tie. Pass --memory-type ({'|'.join(MEMORY_TYPES)}) to say which "
        "one to flash.")


def detect_storage_type(ser) -> dict:
    """Judge this device's storage class over the serial console, pre-flash.

    Returns `{memory_type, disk, source, lsblk}`. `memory_type` goes straight
    to PCAT's -MEMORYTYPE.

    Two lsblk invocations are tried. The first asks for exactly the three
    columns wanted, unsorted and headerless, which is unambiguous to parse;
    util-linux supports it, busybox does not, and a busybox `lsblk` rejects
    the flags rather than ignoring them. The bare second form is the fallback.
    """
    text = ""
    for cmd in ("lsblk -ln -o NAME,TYPE,MOUNTPOINT", "lsblk"):
        try:
            text = run_serial_command(ser, cmd)
        except RuntimeError as e:
            print(f"  ({cmd!r} unusable: {e})")
            continue
        if _parse_lsblk(text):
            break
    else:
        raise RuntimeError(
            "Could not read the block-device list over the serial console "
            "('lsblk' produced nothing usable). Pass --memory-type "
            f"({'|'.join(MEMORY_TYPES)}) to flash without probing.")

    entries = _parse_lsblk(text)
    disk, how = _choose_boot_disk(entries)
    memory_type = _memory_type_for(disk)
    if memory_type is None:
        raise RuntimeError(
            f"Block device {disk!r} does not match any known storage prefix. "
            "Pass --memory-type to flash anyway.")
    return {"memory_type": memory_type, "disk": disk, "source": how,
            "lsblk": text}


# ---------------------------------------------------------------------------
# Alpaca TAC (EDL entry / power control)
# ---------------------------------------------------------------------------

def _open_tac(retries: int = 3, retry_delay_s: float = 2.0):
    import comtypes.client as cc

    # Keep a reference to the top-level AlpacaTACServer COM object at module scope.
    # Create_TAC_Server() returns a *child* COM object (ITACServer); if nothing
    # keeps the parent alive, Python garbage-collects it as soon as this function
    # returns, which disconnects the child's interface and makes every subsequent
    # call (e.g. BootToEDLButton, Close) fail with "The object invoked has
    # disconnected from its clients."
    global _tac_server_ref

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            _tac_server_ref = cc.CreateObject("TACCOM.AlpacaTACServer")
            tac = _tac_server_ref.Create_TAC_Server()
            count = tac.Get_Device_Count()
            if count == 0:
                raise RuntimeError("No Alpaca TAC devices found. Is the debug board connected?")

            port = TAC_PORT_NAME
            if port is None:
                if count > 1:
                    raise RuntimeError(
                        f"Multiple TAC devices found ({count}); pass --tac-port to disambiguate."
                    )
                port = tac.Get_PortName(0)

            if not tac.OpenByName(port):
                raise RuntimeError(f"Failed to open TAC device on port {port}")

            print(f"Opened TAC device: {tac.Get_Name()} ({tac.Get_HardwareVersion()}) on port {port}")
            return tac
        except Exception as e:
            last_error = e
            _tac_server_ref = None
            if attempt < retries:
                print(f"TAC open attempt {attempt}/{retries} failed ({e!r}); retrying in {retry_delay_s}s...")
                time.sleep(retry_delay_s)

    raise RuntimeError(f"Could not open Alpaca TAC server after {retries} attempts: {last_error!r}")


def enter_edl_mode():
    tac = _open_tac()
    try:
        print("Triggering BootToEDL...")
        tac.BootToEDLButton()
    finally:
        tac.Close()


def power_cycle_device():
    """Recover a device stuck in EDL/Sahara (or abort a flash) by power-cycling it
    back to normal boot. The Alpaca TAC has no single 'reset' command -- only
    discrete PowerOffButton / PowerOnButton -- so recovery is off, pause, on."""
    tac = _open_tac()
    try:
        print("Powering device off...")
        tac.PowerOffButton()
        time.sleep(2)
        print("Powering device on...")
        tac.PowerOnButton()
    finally:
        tac.Close()


# ---------------------------------------------------------------------------
# PCAT
# ---------------------------------------------------------------------------

def pcat_list_devices() -> list:
    with tempfile.TemporaryDirectory() as tmp:
        out_file = str(Path(tmp) / "devices.json")
        result = subprocess.run(
            [PCAT_EXE, "-DEVICES", "-JSON", "TRUE", "-OUT", out_file],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"PCAT -DEVICES failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        data = json.loads(Path(out_file).read_text(encoding="utf-8-sig"))
    return data


def wait_for_edl_device(timeout_s: int = 90, poll_interval_s: int = 2) -> dict:
    """Polls PCAT -DEVICES until an EDL-capable device shows up. Right after
    BootToEDL, the device is still re-enumerating over USB, and PCAT -DEVICES
    can transiently fail outright (non-zero exit, not just an empty device
    list) during that window -- tolerate that like any other "not there yet"
    result instead of letting it abort the whole poll loop. A single PCAT
    -DEVICES call has been observed taking ~12s on its own (it does its own
    USB device-manager scan), so the default timeout allows for several
    genuine retries rather than 1-2."""
    deadline = time.time() + timeout_s
    attempt = 0
    last_devices = []
    last_error = None
    while time.time() < deadline:
        attempt += 1
        try:
            devices = pcat_list_devices()
        except RuntimeError as e:
            last_error = e
            print(f"  PCAT -DEVICES attempt {attempt} failed ({e}); retrying ...")
            time.sleep(poll_interval_s)
            continue
        last_devices = devices
        candidates = [
            d for d in devices
            if d.get("device_state", "").upper() not in ("NORMAL",) or d.get("device_type")
        ]
        if candidates:
            return candidates[0]
        print(f"  PCAT -DEVICES attempt {attempt}: no EDL device yet ({time.time() - (deadline - timeout_s):.0f}s elapsed) ...")
        time.sleep(poll_interval_s)
    raise RuntimeError(
        f"No EDL-capable device found via PCAT -DEVICES after {timeout_s}s. "
        f"Last seen: {last_devices}"
        + (f"; last error: {last_error}" if last_error else "")
    )


def pcat_device_id(device: dict) -> str:
    """PCAT's -DEVICES JSON sometimes reports id as the literal string "NA"
    (seen in EDL state on this board) instead of a real identifier -- fall
    back to serial_number, which PCAT does accept as -DEVICE, in that case."""
    device_id = device.get("id")
    if device_id and device_id.upper() != "NA":
        return device_id
    serial_number = device.get("serial_number")
    if serial_number:
        return serial_number
    raise RuntimeError(f"No usable device id or serial_number in PCAT device entry: {device}")


def run_pcat_flash(device_id: str, build_dir: Path,
                   memory_type: str = MEMORY_TYPE_DEFAULT):
    cmd = [
        PCAT_EXE, "-PLUGIN", "SD",
        "-DEVICE", device_id,
        "-BUILD", str(build_dir),
        "-MEMORYTYPE", memory_type,
        "-SLOT", "0",
    ]
    print("\nRunning:", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    print("(Ctrl+C aborts the flash and power-cycles the device back to normal boot)")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for line in proc.stdout:
            print(line, end="")
        proc.wait()
    except KeyboardInterrupt:
        print("\nAborting flash: killing PCAT...")
        proc.kill()
        proc.wait()
        print("PCAT process killed. Power-cycling device via TAC...")
        try:
            power_cycle_device()
        except Exception as e:
            raise RuntimeError(
                f"Flash aborted (PCAT killed), but power-cycling the device via TAC also "
                f"failed: {e!r}. The device may still be in EDL/Sahara mode -- run "
                f"'py -3 bootbench.py flash --recover' to retry, or use the TAC app / physical "
                f"reset directly."
            )
        raise RuntimeError("Flash aborted by user (Ctrl+C); device power-cycled back to normal boot.")

    if proc.returncode != 0:
        raise RuntimeError(f"PCAT flash failed with exit code {proc.returncode}")


# =============================================================================
# Section 2 -- bootchart_report: per-device JSON history + regenerated HTML
# report.
#
# bootchart-data-<slug>.json is the only file ever hand-edited (by appending a
# run). bootchart-overview-<slug>.html is ALWAYS fully derived from the JSON
# by this script -- never hand-authored. Re-running `render` regenerates it
# byte-for-byte from whatever is currently in the JSON.
#
# Run object schema:
# {
#   "timestamp": "YYYY-MM-DD HH:MM",
#   "metrics": {
#     "nhlos":           {"value": "5.035 s", "note": "4.318 s firmware + 0.717 s loader"},
#     "kernel":          {"value": "1.573 s", "note": ""},
#     "initramfs":       {"value": "0.458 s", "note": "0.092 s -> 0.550 s"},
#     "sysinit_svc":     {"value": "2.734 s", "note": "0.550 s -> 3.284 s",
#                          "hitters": [{"name": "systemd-tmpfiles-setup.service", "time": "0.057 s"}, ...]},
#     "total_sysinit":   {"value": "9.342 s", "note": ""},
#     "total_multiuser": {"value": "19.005 s", "note": "~19.79 s incl. graphical.target",
#                          "hitters": [{"name": "android-tools-adbd.service", "time": "10.416 s"}, ...]}
#   },
#   "critical_chain": ["docker.service", "network-online.target", "..."]   // optional, kept for backup/.txt context
# }
# A metric row's `hitters` list (where present) IS rank -- position 0 is #1, NOT a fixed identity
# across runs. NHLOS/kernel/initramfs carry no `hitters` key: there's no per-component timing source
# for them today (no initcall_debug on this build, no bootloader trace).
#
# Older entries from before hitters were merged into the metrics table may still carry a flat
# top-level "hitters" list instead of a nested one. Rendering falls back to treating that as
# total_multiuser's hitters, so no data migration is needed.
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
BOOT_CHARTS_DIR = BASE_DIR / "Boot-Charts"  # flat: every device's bootchart-data/-overview files land directly here, no per-device subdir
DEFAULT_DATA = BOOT_CHARTS_DIR / "bootchart-data.json"
DEFAULT_HTML = BOOT_CHARTS_DIR / "bootchart-overview.html"
BOOT_LOGS_DIR = BASE_DIR / "Boot-Logs"  # nested Boot-Logs/<target>/<build-folder>/<timestamp>.txt

MAX_RUNS = 30  # bootchart-data.json / .html keep only the most recent N runs;
               # full history lives on in Boot-Logs/*.txt regardless of this cap.


def apply_path_overrides(args) -> None:
    """Rebind the output/share path globals from the CLI, once, up front.

    The defaults are anchored to this script's own directory, which is the
    right thing for a hand-run skill and the wrong thing for automation: the
    checkout can live in a synced folder (OneDrive locks files mid-write and
    an adb pull into one fails in confusing ways), and a bench host wants
    artifacts somewhere bounded and backed up on its own terms.

    Must be called before anything reads these -- see boot_logs_run_dir() and
    render() on the late-binding default-argument trap that made overriding
    them silently ineffective.
    """
    global BOOT_CHARTS_DIR, DEFAULT_DATA, DEFAULT_HTML, BOOT_LOGS_DIR, YOCTO_SHARE

    charts = getattr(args, "boot_charts_dir", None)
    if charts:
        BOOT_CHARTS_DIR = Path(charts).expanduser()
        # Derived, so they must be recomputed rather than left pointing into
        # the old directory.
        DEFAULT_DATA = BOOT_CHARTS_DIR / "bootchart-data.json"
        DEFAULT_HTML = BOOT_CHARTS_DIR / "bootchart-overview.html"

    logs = getattr(args, "boot_logs_dir", None)
    if logs:
        BOOT_LOGS_DIR = Path(logs).expanduser()

    share = getattr(args, "share_root", None)
    if share:
        YOCTO_SHARE = share

METRIC_ROWS = [
    ("nhlos", "NHLOS (PBL/SBL/XBL firmware + ABL loader)", False),
    ("kernel", "Kernel time", False),
    ("initramfs", "Initramfs (<code>Run /init</code> &rarr; epoch advance)", False),
    ("sysinit_svc", "systemd &rarr; sysinit.target", False),
    ("total_sysinit", "Total time till sysinit.target", True),
    ("total_multiuser", "Total time till multi-user.target", True),
]

# Notes that are identical on every boot (constant text, not derived per-run)
# are shown once under the row's own label instead of being repeated in every
# cell -- see render_metrics_table / _hitters_html.
ROW_CAPTIONS = {
    "initramfs": "dmesg: Run /init → System time advanced to built-in epoch",
    "sysinit_svc": "dmesg epoch advance → journalctl: Reached target System Initialization",
}

DELTA_THRESHOLD = 0.5
TIME_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_seconds(value):
    if not value:
        return None
    m = TIME_RE.search(value)
    return float(m.group()) if m else None


def format_delta(prev_value, cur_value):
    """Return (label, css_class) for the delta annotation, or ("", "") if not computable."""
    p, c = parse_seconds(prev_value), parse_seconds(cur_value)
    if p is None or c is None:
        return "", ""
    delta = c - p
    if delta >= DELTA_THRESHOLD:
        cls = "regressed"
    elif delta <= -DELTA_THRESHOLD:
        cls = "improved"
    else:
        cls = "unchanged"
    sign = "+" if delta >= 0 else "−"
    return f"({sign}{abs(delta):.3f} s)", cls


def esc(value):
    return html.escape(str(value), quote=False)


def load_data(data_path):
    if not data_path.exists():
        return {"device": "unknown", "runs": []}
    with open(data_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data, data_path):
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _validate_boot(boot):
    required = {"timestamp", "metrics"}
    missing = required - boot.keys()
    if missing:
        raise ValueError(f"boot object missing required keys: {missing}")
    missing_metrics = {k for k, _, _ in METRIC_ROWS} - boot["metrics"].keys()
    if missing_metrics:
        raise ValueError(f"boot.metrics missing required keys: {missing_metrics}")


def add_run(data, run):
    if "boots" in run:
        required_top = {"timestamp", "build_path", "boots"}
        missing = required_top - run.keys()
        if missing:
            raise ValueError(f"multi-boot entry missing required keys: {missing}")
        if not run["boots"]:
            raise ValueError("multi-boot entry has an empty 'boots' list")
        for boot in run["boots"]:
            _validate_boot(boot)
    else:
        _validate_boot(run)
    runs = data.setdefault("runs", [])
    runs.append(run)
    del runs[:-MAX_RUNS]  # trim from the front; full history is preserved in Boot-Logs/*.txt
    return data


def _flatten(runs):
    """Expands entries into a flat list of per-boot records, normalizing
    legacy flat (single-boot) entries to a 1-item boots list. Each record:
    {"boot": <boot dict>, "idx": <position within its entry>, "n": <boots in that entry>}.
    All rendering iterates this flat list so delta/rank/footer logic doesn't
    need to special-case entry boundaries -- boot 0 of entry K just diffs
    against the last boot of entry K-1, like any two consecutive boots."""
    flat = []
    for run in runs:
        boots = run.get("boots") or [run]
        n = len(boots)
        for idx, boot in enumerate(boots):
            flat.append({"boot": boot, "idx": idx, "n": n})
    return flat


def _metric_hitters(boot, key):
    """Returns the hitters list for a given metric row, or [] if none.
    Legacy entries (captured before hitters were merged into the metrics
    table) carry a flat top-level "hitters" list -- treated as
    total_multiuser's hitters, since that's what it always meant before."""
    m = boot["metrics"].get(key, {})
    if "hitters" in m:
        return m["hitters"]
    if key == "total_multiuser":
        return boot.get("hitters") or []
    return []


def _format_run_txt(target, run):
    """Human-readable dump of a single run, for the Boot-Logs/ backup -- the
    full-history record that survives bootchart-data.json's MAX_RUNS trim."""
    lines = [
        f"Device: {target}",
        f"Timestamp: {run['timestamp']}",
        f"Build path: {run.get('build_path', '')}",
    ]
    if run.get("phase"):
        lines.append(f"Phase: {run['phase']} (boot {run.get('log_name', '').rsplit('-', 1)[-1]})")
    lines += ["", "Metrics:"]
    for key, label, _ in METRIC_ROWS:
        m = run["metrics"].get(key, {})
        note = f"  ({m['note']})" if m.get("note") else ""
        lines.append(f"  {label}: {m.get('value', '')}{note}")
        for i, h in enumerate(_metric_hitters(run, key)):
            h_note = f"  ({h['note']})" if h.get("note") else ""
            lines.append(f"    #{i + 1}. {h['name']}: {h['time']}{h_note}")

    chain = run.get("critical_chain")
    if chain:
        lines.append("")
        lines.append("Critical chain: " + " -> ".join(chain))

    overall_overheads = run.get("overall_overheads")
    if overall_overheads:
        lines.append("")
        lines.append("Overall Suspected Overheads:")
        for i, h in enumerate(overall_overheads):
            h_note = f"  ({h['note']})" if h.get("note") else ""
            lines.append(f"  #{i + 1}. {h['name']}: {h['time']}{h_note}")

    if run.get("optimization_possibilities"):
        lines.append("")
        lines.append("Optimization possibilities: " + run["optimization_possibilities"])

    if run.get("source"):
        lines.append("")
        lines.append("Source: " + run["source"])

    return "\n".join(lines) + "\n"


def boot_logs_run_dir(target, build_path, boot_logs_dir=None):
    """Returns the nested Boot-Logs/<target>/<build-folder-name> directory for
    a given target + build_path, shared by save_run_backup's per-boot .txt
    backups and boot-capture's pulled raw-log folders (Logs-1, Logs-2, ...) so
    both land side-by-side in the same directory.

    boot_logs_dir defaults to None, not to BOOT_LOGS_DIR: a default argument
    is evaluated once at def time, so binding the constant here would have
    pinned the original path and made --boot-logs-dir silently ineffective."""
    if boot_logs_dir is None:
        boot_logs_dir = BOOT_LOGS_DIR
    return boot_logs_dir / target / _build_folder_name(build_path) if build_path else boot_logs_dir


def save_run_backup(target, run, boot_logs_dir=None):
    """Writes a permanent per-run backup to
    Boot-Logs/<target>/<build-folder-name>/<timestamp>.txt, since
    bootchart-data.json only retains the most recent MAX_RUNS entries. Adds a
    numeric suffix if a backup for the same minute-granularity timestamp
    already exists (e.g. consecutive boots in a multi-boot entry can finish
    within the same minute), so no boot's backup is silently overwritten.
    Pre-existing flat backups directly under boot_logs_dir (from before this
    nested layout) are left in place -- only new backups go into the nested
    <target>/<build-folder> path.

    See boot_logs_run_dir on why boot_logs_dir defaults to None."""
    if boot_logs_dir is None:
        boot_logs_dir = BOOT_LOGS_DIR
    build_path = run.get("build_path", "")
    run_dir = boot_logs_run_dir(target, build_path, boot_logs_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = run["timestamp"].replace(":", "-")
    txt_path = run_dir / f"{safe_ts}.txt"
    n = 2
    while txt_path.exists():
        txt_path = run_dir / f"{safe_ts}_{n}.txt"
        n += 1
    txt_path.write_text(_format_run_txt(target, run), encoding="utf-8")
    return txt_path


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
  :root {
    --bg: #ffffff;
    --panel: #f7f8fa;
    --border: #000000;
    --text: #1c1f26;
    --muted: #6b7280;
    --accent: #2563eb;
    --good: #1a9c63;
    --warn: #b8860b;
    --bad: #d1403f;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
    overflow-x: auto;
  }
  .container { padding: 40px 20px; width: max-content; min-width: 100%; }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  .subtitle { color: var(--muted); font-size: 0.95rem; margin-bottom: 28px; max-width: 1100px; }
  .subtitle code { background: var(--panel); padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border); }
  h2 { font-size: 1.1rem; margin-top: 36px; margin-bottom: 12px; border-left: 3px solid var(--accent); padding-left: 10px; }
  table { border-collapse: collapse; background: var(--bg); border: 2px solid var(--border); }
  th, td { text-align: left; padding: 12px 19px; border: 1px solid var(--border); white-space: nowrap; }
  th { background: var(--panel); color: var(--muted); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.04em; }
  th.run-col { color: var(--accent); text-transform: none; letter-spacing: normal; font-size: 0.85rem; min-width: 520px; }
  th.run-col.latest { color: var(--text); background: #e8eefc; }
  tr.total td { font-weight: 600; color: var(--accent); background: #eef2fd; }
  td.timing { font-family: "SF Mono", Consolas, monospace; font-size: 0.95rem; }
  td.timing .note { display: block; color: var(--muted); font-size: 0.78rem; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; white-space: normal; margin-top: 2px; }
  td.timing .delta { font-size: 0.78rem; margin-left: 6px; font-family: "SF Mono", Consolas, monospace; }
  .delta.unchanged { color: var(--muted); }
  .delta.improved { color: var(--good); }
  .delta.regressed { color: var(--bad); }
  td.buildpath { font-family: "SF Mono", Consolas, monospace; font-size: 0.78rem; color: var(--muted); white-space: normal; word-break: break-all; text-align: center; }
  th.entry-start, td.entry-start { border-left: 3px solid var(--accent); padding-left: 20px; }
  th.phase-start, td.phase-start { padding-left: 20px; }
  td:first-child .row-caption { display: block; font-weight: 400; font-size: 0.75rem; color: var(--muted); margin-top: 2px; white-space: normal; }
  th .row-caption { display: block; font-weight: 400; font-size: 0.75rem; color: var(--muted); margin-top: 2px; white-space: normal; }
  td.timing ol.hitters { list-style: none; margin: 6px 0 0; padding: 0; white-space: normal; }
  td.timing ol.hitters li { font-size: 0.78rem; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: var(--muted); margin-top: 2px; }
  td.timing ol.hitters li .hitter-name { font-family: "SF Mono", Consolas, monospace; }
  td.optimization { white-space: normal; max-width: 420px; vertical-align: top; }
  td.optimization .optimization-text { font-size: 0.85rem; line-height: 1.5; white-space: normal; word-wrap: break-word; }
  td.optimization ul.optimization-text { margin: 0; padding-left: 18px; }
  td.optimization ul.optimization-text li { margin-bottom: 6px; }
  td.na { color: var(--muted); text-align: center; }
"""


def _boot_header_label(rec):
    boot = rec["boot"]
    if rec["n"] == 1:
        return boot["timestamp"]
    log_name = boot.get("log_name")
    if log_name:
        phase, _n = log_name.rsplit("-", 1)
        return phase.capitalize()
    time_part = boot["timestamp"].split(" ")[-1]
    return f"Boot {rec['idx'] + 1} · {time_part}"


def _boot_header_subtitle(rec):
    """Returns a small subtitle string to show under a column header, or
    None. Content-based (checks metrics.kernel.hitters), not a threaded
    flag -- correct even under --resume-pull, which never calls
    ensure_debug_cmdline_params() at all."""
    boot = rec["boot"]
    if boot.get("phase") != "debug":
        return None
    if boot.get("metrics", {}).get("kernel", {}).get("hitters"):
        return None
    return "No debug prints enabled"


def _phase_boundary(flat, i):
    """True when boot i starts a new phase (e.g. debug right after default)
    within the same multi-boot entry -- used to draw a visual separator
    between the default and debug columns, distinct from entry-start's
    separator between different captured runs."""
    if i == 0:
        return False
    prev_phase = flat[i - 1]["boot"].get("phase")
    cur_phase = flat[i]["boot"].get("phase")
    return bool(prev_phase and cur_phase and prev_phase != cur_phase)


def _col_extra_class(flat, i, rec):
    classes = []
    if rec["idx"] == 0 and i > 0:
        classes.append("entry-start")
    elif _phase_boundary(flat, i):
        classes.append("phase-start")
    return (" " + " ".join(classes)) if classes else ""


def _header_cells(flat):
    return "".join(
        f'<th class="run-col{" latest" if i == len(flat) - 1 else ""}'
        f'{_col_extra_class(flat, i, rec)}">'
        f'{esc(_boot_header_label(rec))}'
        + (
            f'<span class="row-caption">{esc(_boot_header_subtitle(rec))}</span>'
            if _boot_header_subtitle(rec) else ""
        )
        + '</th>'
        for i, rec in enumerate(flat)
    )


def _strip_performance_suffix(build_path):
    """Boot analysis is only ever run against a build's '...\\performance'
    subdirectory, so that segment is implied and dropped from display/paths."""
    p = PureWindowsPath(build_path)
    if p.name.lower() == "performance":
        p = p.parent
    return str(p)


def _build_folder_name(build_path):
    return PureWindowsPath(_strip_performance_suffix(build_path)).name or "unknown-build"


def render_build_path_row(runs):
    if not any(r.get("build_path") for r in runs):
        return ""
    cells = "".join(
        f'<td class="buildpath" colspan="{len(run.get("boots") or [run])}">{esc(_strip_performance_suffix(run.get("build_path", "")))}</td>'
        for run in runs
    )
    return f"<tr><td>Nightly build path</td>{cells}</tr>"


def _hitters_html(hitters):
    """Ranked sub-list rendered inside a metric's value cell. Each hitter's
    "note" (e.g. "largest single cost", "critical path"/"off critical path")
    is precomputed at capture time in build_run, not here."""
    if not hitters:
        return ""
    items = []
    for i, h in enumerate(hitters):
        note = h.get("note", "")
        note_html = f" &middot; {esc(note)}" if note else ""
        items.append(
            f'<li class="rank-{i + 1}">#{i + 1} <span class="hitter-name">{esc(h["name"])}</span> '
            f'&mdash; {esc(h["time"])}{note_html}</li>'
        )
    return f'<ol class="hitters">{"".join(items)}</ol>'


def _optimization_html(text):
    """Renders optimization_possibilities as a bulleted list, one <li> per
    newline-separated point (each point names a service/probe hitter and its
    suggested fix -- see SKILL.md workflow step 4). Falls back to a single
    wrapped block for older entries that are still one paragraph (no
    newlines)."""
    lines = [l.strip().lstrip("•").strip() for l in text.splitlines() if l.strip()]
    if len(lines) <= 1:
        return f'<div class="optimization-text">{esc(text)}</div>'
    items = "".join(f"<li>{esc(line)}</li>" for line in lines)
    return f'<ul class="optimization-text">{items}</ul>'


def render_overall_rows(flat):
    """Two rows appended below the per-stage metric rows: 'Overall Suspected
    Overheads' (the pooled, cross-stage top hitters computed in build_run)
    and 'Optimization Possibilities' (an agent-filled placeholder, present
    only on debug boots that had kernel hitters)."""
    overhead_cells = []
    optimization_cells = []
    for i, rec in enumerate(flat):
        boot = rec["boot"]
        cls = _col_extra_class(flat, i, rec)
        overheads = boot.get("overall_overheads") or []
        overhead_html = _hitters_html(overheads) if overheads else '<span class="na">–</span>'
        overhead_cells.append(f'<td class="timing{cls}">{overhead_html}</td>')

        optimization = boot.get("optimization_possibilities")
        optimization_html = _optimization_html(optimization) if optimization else '<span class="na">–</span>'
        optimization_cells.append(f'<td class="timing optimization{cls}">{optimization_html}</td>')

    overhead_row = f"<tr><td>Overall Suspected Overheads</td>{''.join(overhead_cells)}</tr>"
    optimization_row = f"<tr><td>Optimization Possibilities</td>{''.join(optimization_cells)}</tr>"
    return overhead_row + optimization_row


def render_metrics_table(runs):
    flat = _flatten(runs)
    header_cells = _header_cells(flat)
    rows_html = [render_build_path_row(runs)]
    for key, label, is_total in METRIC_ROWS:
        caption = ROW_CAPTIONS.get(key)
        label_html = f'{label}<span class="row-caption">{esc(caption)}</span>' if caption else label
        cells = []
        for i, rec in enumerate(flat):
            boot = rec["boot"]
            m = boot["metrics"][key]
            value, note = m.get("value", ""), m.get("note", "")
            delta_html = ""
            if i > 0:
                prev_value = flat[i - 1]["boot"]["metrics"][key].get("value", "")
                delta_label, delta_cls = format_delta(prev_value, value)
                if delta_label:
                    delta_html = f'<span class="delta {delta_cls}">{delta_label}</span>'
            note_html = f'<span class="note">{esc(note)}</span>' if note and note != caption else ""
            hitters_html = _hitters_html(_metric_hitters(boot, key))
            cls = "timing" + _col_extra_class(flat, i, rec)
            cells.append(f'<td class="{cls}">{esc(value)}{delta_html}{note_html}{hitters_html}</td>')
        row_class = ' class="total"' if is_total else ""
        rows_html.append(f"<tr{row_class}><td>{label_html}</td>{''.join(cells)}</tr>")
    rows_html.append(render_overall_rows(flat))
    return f"""
  <table>
    <thead><tr><th>Stage</th>{header_cells}</tr></thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>"""


def render_html(data):
    runs = data.get("runs", [])
    device = esc(data.get("device", "unknown device"))
    if not runs:
        body = "<p>No runs collected yet. Run <code>bootbench.py report add-run &lt;run.json&gt;</code> to add the first one.</p>"
        metrics_html = ""
    else:
        metrics_html = render_metrics_table(runs)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Boot Time Analysis &mdash; {device}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

  <h1>Boot Time Analysis</h1>
  <div class="subtitle">Device <code>{device}</code> &mdash; history of collected boot runs. Data source: <code>bootchart-data.json</code>, generated by <code>bootbench.py</code>. <b>Boot analysis is performed only on performance builds.</b> High hitters for each stage (where measurable) are ranked inline beneath that stage's timing.</div>

  <h2>Boot Time Summary</h2>
  {metrics_html}

</div>
</body>
</html>
"""


def render(data_path=None, html_path=None):
    # None-defaulted rather than bound to DEFAULT_DATA/DEFAULT_HTML: those are
    # derived from BOOT_CHARTS_DIR, which --boot-charts-dir rebinds, and a
    # default argument would have captured the pre-override value at def time.
    if data_path is None:
        data_path = DEFAULT_DATA
    if html_path is None:
        html_path = DEFAULT_HTML
    data = load_data(data_path)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(data), encoding="utf-8")
    print(f"Rendered {html_path} from {data_path} ({len(data.get('runs', []))} run(s)).")


# =============================================================================
# Section 3 -- boot_capture: collects boot-time metrics from a target device
# over N consecutive boots and records them via Section 2's JSON+HTML report.
#
# Per boot, a device-side script (collect_boot_logs.sh, provisioned to
# /data/collect_boot_logs.sh on first use) dumps systemd-analyze/blame/dmesg/etc.
# into /data/Logs, which is then renamed to /data/Logs-<n> so each boot's dump
# survives the next boot's run. Login, script provisioning/execution, and the
# per-boot rename all happen over the serial console (open_console /
# run_serial_command from Section 1) -- no adb/USB involved for that part. Only
# after all boots are done is adb enabled (touch /etc/usb-debugging-enabled;
# systemctl start android-tools-adbd) for a single `adb pull` of every Logs-<n>
# folder into Boot-Logs/<target>/<build-folder>/, which is then parsed to build
# each boot's metrics.
#
# Normally invoked automatically by the `flash` subcommand right after a
# successful flash. Can also be run standalone (`capture` subcommand) to
# (re-)capture without reflashing.
# =============================================================================

DELTA_MERGE_S = 0.05  # threshold for noting graphical.target overshoot

COLLECT_SCRIPT_REMOTE_PATH = "/data/collect_boot_logs.sh"
COLLECT_SCRIPT_HEREDOC_MARKER = "BOOT_LOG_SCRIPT_EOF"

COLLECT_SCRIPT_CONTENT = """#!/bin/sh
# collect_boot_logs.sh
# This script runs locally on the target to collect boot optimization logs.

echo "=========================================="
echo "    Target Boot Log Collection Script     "
echo "=========================================="

setenforce 0

echo "[0/4] Disabling Remote FS"
systemctl disable rmtfs.service
systemctl mask rmtfs.service

#echo "[1/4] Disabling Network Wait-Online Services..."
#systemctl stop systemd-networkd-wait-online.service 2>/dev/null
#systemctl disable systemd-networkd-wait-online.service 2>/dev/null
#systemctl disable NetworkManager-wait-online.service 2>/dev/null
#systemctl mask NetworkManager-wait-online.service 2>/dev/null

echo "[2/4] Preparing /data/Logs directory..."
mkdir -p /data/Logs
rm -f /data/Logs/*

echo "Turning off kernel tracing..."
echo 0 > /sys/kernel/tracing/tracing_on 2>/dev/null || true

echo "[3/4] Collecting System Logs..."
cp /sys/kernel/tracing/trace /data/Logs/Boot_Trace.txt 2>/dev/null || true
systemd-analyze > /data/Logs/systemd_analyze.txt
systemd-analyze blame > /data/Logs/blame_systemd.txt
systemd-analyze plot > /data/Logs/plot_systemd.svg
systemctl > /data/Logs/blame_systemdsystemctl.txt
journalctl --output=short-monotonic -b --no-pager -l > /data/Logs/journalctl.log
dmesg > /data/Logs/dmesg.txt
systemd-analyze critical-chain > /data/Logs/systemd-analyze_critical_chain.txt
systemd-analyze critical-chain sysinit.target > /data/Logs/systemd-analyze_critical_chain_sysinit.txt

# Extract config if present
if [ -f /proc/config.gz ]; then
    zcat /proc/config.gz > /data/Logs/proc_config.txt
fi

cat /proc/cmdline > /data/Logs/kernel_cmdline.txt
lsmod > /data/Logs/lsmod.txt

echo
echo "------------------------------------------"
cat /data/Logs/systemd_analyze.txt
echo "------------------------------------------"

echo "[4/4] Generating Summary (brief.txt)..."
"""


def _slugify(target: str) -> str:
    return target.replace("-", "").replace("_", "")


def _data_html_paths(target: str):
    slug = _slugify(target)
    base = BOOT_CHARTS_DIR
    return base / f"bootchart-data-{slug}.json", base / f"bootchart-overview-{slug}.html"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

TIME_TOKEN_RE = re.compile(r"([\d.]+)(ms|s)")


def _to_seconds(num: str, unit: str) -> float:
    value = float(num)
    return value / 1000.0 if unit == "ms" else value


def parse_systemd_time(text: str) -> dict:
    """Parses the 'Startup finished in ...' line from `systemd-analyze time`."""
    m = re.search(
        r"Startup finished in (.+?)\s*=\s*([\d.]+)(ms|s)",
        text,
    )
    if not m:
        raise RuntimeError(f"Could not parse `systemd-analyze time` output:\n{text}")
    parts_text, total_num, total_unit = m.group(1), m.group(2), m.group(3)

    parts = {}
    for num, unit, label in re.findall(r"([\d.]+)(ms|s)\s*\((\w+)\)", parts_text):
        parts[label] = _to_seconds(num, unit)

    result = {
        "firmware": parts.get("firmware", 0.0),
        "loader": parts.get("loader", 0.0),
        "kernel": parts.get("kernel", 0.0),
        "userspace": parts.get("userspace", 0.0),
        "total": _to_seconds(total_num, total_unit),
    }

    gm = re.search(r"graphical\.target reached after ([\d.]+)(ms|s)", text)
    if gm:
        result["graphical_userspace"] = _to_seconds(gm.group(1), gm.group(2))
    return result


def parse_critical_chain(text: str) -> tuple:
    """Returns (top_level_userspace_seconds, chain_names_top_to_bottom).
    The '@Xs' on each line is time-since-userspace-start (PID 1), NOT
    time-since-power-on -- confirmed against real device output where the
    top target's @ value matches systemd-analyze time's userspace figure."""
    lines = [l for l in text.splitlines() if re.search(r"@[\d.]+(?:ms|s)", l)]
    if not lines:
        raise RuntimeError(f"Could not parse `systemd-analyze critical-chain` output:\n{text}")

    top_m = re.search(r"@([\d.]+)(ms|s)", lines[0])
    if not top_m:
        raise RuntimeError(f"Could not find '@time' on first critical-chain line: {lines[0]!r}")
    top_seconds = _to_seconds(top_m.group(1), top_m.group(2))

    chain = []
    for line in lines:
        m = re.search(r"[`\-\s]*([\w.@:\\+-]+\.(?:service|target|socket|device|mount|slice))\s*@", line)
        if m:
            chain.append(m.group(1))
    return top_seconds, chain


STAGE_HITTER_RE = re.compile(
    r"[`\-\s]*([\w.@:\\+-]+\.(?:service|target|socket|device|mount|slice))"
    r"\s*@[\d.]+(?:ms|s)\s+\+([\d.]+)(ms|s)"
)


def parse_stage_hitters(text: str, top_n: int = 6) -> list:
    """Ranks units in a `systemd-analyze critical-chain` block by their own
    '+cost' (time the unit itself took to start), descending. Unlike
    parse_critical_chain's `chain` (top-to-bottom dependency order), this is
    a ranking -- used for per-stage high hitters (e.g. sysinit_svc), where
    there's no `blame`-style command scoped to a single target."""
    costs = []
    for m in STAGE_HITTER_RE.finditer(text):
        name, num, unit = m.group(1), m.group(2), m.group(3)
        costs.append((_to_seconds(num, unit), name))
    costs.sort(key=lambda t: t[0], reverse=True)
    return [{"name": name, "time": f"{secs:.3f} s"} for secs, name in costs[:top_n]]


def parse_blame(text: str, top_n: int = 6) -> list:
    hitters = []
    for line in text.splitlines():
        m = re.match(r"\s*([\d.]+)(ms|s)\s+(\S+)", line)
        if not m:
            continue
        seconds = _to_seconds(m.group(1), m.group(2))
        hitters.append({"name": m.group(3), "time": f"{seconds:.3f} s"})
        if len(hitters) >= top_n:
            break
    if not hitters:
        raise RuntimeError(f"Could not parse `systemd-analyze blame` output:\n{text}")
    return hitters


def parse_dmesg_milestones(text: str) -> dict:
    def find(pattern):
        m = re.search(pattern, text)
        if not m:
            raise RuntimeError(f"dmesg milestone not found ({pattern!r}) in:\n{text}")
        return float(m.group(1))

    return {
        "init_exec": find(r"\[\s*([\d.]+)\]\s*Run /init as init process"),
        "epoch_advanced": find(r"\[\s*([\d.]+)\]\s*systemd\[1\]: System time advanced to built-in epoch"),
        "systemd_running": find(r"\[\s*([\d.]+)\]\s*systemd\[1\]: systemd .* running in system mode"),
    }


INITCALL_PROBE_RE = re.compile(r"(?:initcall|probe of) (\S+) returned (-?\d+) after (\d+) usecs")
PROBE_FAIL_RE = re.compile(r"(\S+): probe with driver \S+ failed with error (-?\d+)")


def parse_kernel_hitters(dmesg_text: str, top_n_slow: int = 3, max_failures: int = 3):
    """Extracts debug-cmdline-derived kernel init signal from a debug-phase
    dmesg (requires initcall_debug to be active -- see ensure_debug_cmdline_params):
    the top_n_slow slowest probes/initcalls by usecs, followed by up to
    max_failures probe/initcall failures (deduped by name). Returns None if
    dmesg carries no initcall/probe timing at all -- a content-based signal
    that debug cmdline params weren't active for this boot, correct
    regardless of the reason (Type #2 bootloader, --resume-pull, etc.)."""
    probe_matches = list(INITCALL_PROBE_RE.finditer(dmesg_text))
    fail_matches = list(PROBE_FAIL_RE.finditer(dmesg_text))
    if not probe_matches and not fail_matches:
        return None

    slow = sorted(
        ((m.group(1), int(m.group(2)), int(m.group(3))) for m in probe_matches),
        key=lambda t: t[2], reverse=True,
    )[:top_n_slow]
    slow_list = [
        {"name": name, "time": f"{usecs / 1_000_000:.3f} s", "note": "slowest probe/initcall"}
        for name, _rc, usecs in slow
    ]

    failures = []
    seen = set()
    for m in probe_matches:
        name, rc, _usecs = m.group(1), m.group(2), m.group(3)
        if rc == "0" or name in seen:
            continue
        seen.add(name)
        failures.append({"name": name, "time": f"rc={rc}", "note": "probe/initcall failure"})
    for m in fail_matches:
        name, rc = m.group(1), m.group(2)
        if name in seen:
            continue
        seen.add(name)
        failures.append({"name": name, "time": f"error {rc}", "note": "probe failure"})
    failures = failures[:max_failures]

    return slow_list + failures


def parse_journalctl_milestone(text: str) -> float:
    """Returns the monotonic timestamp of 'Reached target System
    Initialization.' from journalctl --output=short-monotonic -- this
    milestone isn't reliably present in dmesg's own ring buffer, unlike the
    epoch-advance line, so it's read from the journal instead."""
    m = re.search(r"\[\s*([\d.]+)\]\s*.*Reached target System Initialization\.", text)
    if not m:
        raise RuntimeError(
            f"journalctl milestone not found ('Reached target System Initialization.') in:\n{text}"
        )
    return float(m.group(1))


def _sec3(value):
    """Round to the same 3 decimals every displayed value already uses.

    Deliberately `float(f"{value:.3f}")` rather than `round(value, 3)`: this
    is defined as "the number the report shows, as a float", so taking it
    through the identical formatting step makes that true by construction
    instead of relying on round() and format() agreeing at a half-way case.
    """
    if value is None:
        return None
    return float(f"{float(value):.3f}")


def build_run(target: str, build_path: str, sat_text: str, cc_text: str,
              cc_sysinit_text: str, blame_text: str, dmesg_text: str, journalctl_text: str) -> dict:
    sat = parse_systemd_time(sat_text)
    cc_multiuser_s, chain = parse_critical_chain(cc_text)
    hitters = parse_blame(blame_text)
    sysinit_hitters = parse_stage_hitters(cc_sysinit_text)
    kernel_hitters = parse_kernel_hitters(dmesg_text)
    milestones = parse_dmesg_milestones(dmesg_text)
    sysinit_target_s = parse_journalctl_milestone(journalctl_text)

    nhlos = sat["firmware"] + sat["loader"]
    kernel = sat["kernel"]

    init_exec = milestones["init_exec"]
    epoch_advanced = milestones["epoch_advanced"]
    initramfs_s = epoch_advanced - init_exec
    sysinit_svc = sysinit_target_s - epoch_advanced

    total_sysinit = nhlos + kernel + initramfs_s + sysinit_svc
    total_multiuser = nhlos + kernel + cc_multiuser_s

    multiuser_note = ""
    grand_total = nhlos + kernel + sat["userspace"]
    if grand_total - total_multiuser >= DELTA_MERGE_S:
        multiuser_note = f"≈{grand_total:.3f} s incl. graphical.target"

    kernel_metric = {"value": f"{kernel:.3f} s", "note": ""}
    if kernel_hitters:
        kernel_metric["hitters"] = kernel_hitters

    def _hitter_seconds(h):
        m = TIME_RE.search(h.get("time", ""))
        return float(m.group()) if m else -1.0

    pool = []
    for source_hitters in (kernel_hitters or [], sysinit_hitters, hitters):
        pool.extend(source_hitters)
    pool = [h for h in pool if _hitter_seconds(h) >= 0]
    pool.sort(key=_hitter_seconds, reverse=True)
    overall_overheads = pool[:3]

    run = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "build_path": build_path,
        "metrics": {
            "nhlos": {
                "value": f"{nhlos:.3f} s",
                "note": f"{sat['firmware']:.3f} s firmware + {sat['loader']:.3f} s loader",
            },
            "kernel": kernel_metric,
            "initramfs": {
                "value": f"{initramfs_s:.3f} s",
                "note": f"{init_exec:.3f} s → {epoch_advanced:.3f} s",
            },
            "sysinit_svc": {
                "value": f"{sysinit_svc:.3f} s",
                "note": f"{epoch_advanced:.3f} s → {sysinit_target_s:.3f} s",
                "hitters": sysinit_hitters,
            },
            "total_sysinit": {"value": f"{total_sysinit:.3f} s", "note": ""},
            "total_multiuser": {
                "value": f"{total_multiuser:.3f} s",
                "note": multiuser_note,
                "hitters": hitters,
            },
        },
        "critical_chain": chain,
        "overall_overheads": overall_overheads,
        "optimization_possibilities": None,
        # Numeric mirror of the formatted values above, plus the
        # sub-components that previously existed only inside the prose
        # "note"/"source" strings. Additive and advisory: every consumer in
        # this file reads "metrics" and ignores this key, and _validate_boot
        # checks only for the presence of required keys, so an older report
        # without it still loads. It exists so a downstream database does not
        # have to re-parse "1.573 s" back into a float -- see
        # run_metrics_seconds() for reading history recorded before it.
        #
        # Every value goes through _sec3, so it is exactly the displayed
        # 3-decimal string parsed back to a float. Storing the raw arithmetic
        # would put 5.034999999999999 here next to "5.035 s" in the same
        # report, and a numeric mirror that disagrees with the number a human
        # reads is worse than no mirror at all.
        "seconds": {
            "nhlos": _sec3(nhlos),
            "kernel": _sec3(kernel),
            "initramfs": _sec3(initramfs_s),
            "sysinit_svc": _sec3(sysinit_svc),
            "total_sysinit": _sec3(total_sysinit),
            "total_multiuser": _sec3(total_multiuser),
            "grand_total": _sec3(grand_total),
            "firmware": _sec3(sat["firmware"]),
            "loader": _sec3(sat["loader"]),
            "userspace": _sec3(sat["userspace"]),
            "sat_total": _sec3(sat["total"]),
            "init_exec": _sec3(init_exec),
            "epoch_advanced": _sec3(epoch_advanced),
            "systemd_running": _sec3(milestones["systemd_running"]),
            "sysinit_target": _sec3(sysinit_target_s),
            "cc_multiuser": _sec3(cc_multiuser_s),
        },
        "source": (
            f"systemd-analyze time -> Startup finished in {sat['firmware']:.3f}s (firmware) + "
            f"{sat['loader']:.3f}s (loader) + {sat['kernel']:.3f}s (kernel) + "
            f"{sat['userspace']:.3f}s (userspace) = {sat['total']:.3f}s; "
            f"dmesg: Run /init @{init_exec:.3f}s, System time advanced to built-in epoch @{epoch_advanced:.3f}s, "
            f"systemd running @{milestones['systemd_running']:.3f}s; "
            f"journalctl: Reached target System Initialization @{sysinit_target_s:.3f}s; "
            f"initramfs = Run/init -> epoch-advance; sysinit_svc = epoch-advance -> sysinit.target; "
            f"sysinit hitters from `systemd-analyze critical-chain sysinit.target` "
            f"(+cost per unit); multi-user hitters from `systemd-analyze blame`; "
            f"kernel hitters (debug boots only) from initcall_debug dmesg output; "
            f"overall_overheads pools kernel/sysinit/multi-user hitters and ranks by actual duration"
        ),
    }
    return run


def run_metrics_seconds(run: dict) -> dict:
    """Numeric view of one boot's metrics.

    Prefers the "seconds" key build_run now emits. For boots recorded before
    that key existed, falls back to parse_seconds() over the formatted
    METRIC_ROWS values, so a report written by an older copy of this script
    is still readable numerically -- the six METRIC_ROWS metrics are
    recoverable that way, the sub-components are not, and come back absent
    rather than as a wrong zero.

    Values are floats; a metric that could not be parsed is omitted entirely,
    never defaulted. A missing boot time and a 0.000 s boot time are very
    different claims.
    """
    seconds = run.get("seconds")
    if isinstance(seconds, dict):
        out = {}
        for key, value in seconds.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            out[key] = round(float(value), 3)
        if out:
            return out

    out = {}
    for key, _label, _is_total in METRIC_ROWS:
        parsed = parse_seconds((run.get("metrics", {}).get(key) or {}).get("value"))
        if parsed is not None:
            out[key] = round(parsed, 3)
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

DEBUG_CMDLINE_HEREDOC_MARKER = "DEBUG_CMDLINE_EOF"

BOOTCTL_SOURCE_RE = re.compile(r"source:\s*(\S+)")

# name-only presence check: any value counts as satisfied (the user may
# want a different buffer size than our default).
LOG_BUF_LEN_RE = re.compile(r"(?<![^\s])log_buf_len=\S+")
LOG_BUF_LEN_PARAM = "log_buf_len=4M"

# exact-match checks: token must be present verbatim (initcall_debug) or
# with exactly this value (systemd.log_level=debug) -- unlike log_buf_len
# above, a differing value does not count as already satisfied.
EXACT_CMDLINE_PARAMS = [
    (re.compile(r"(?<![^\s])initcall_debug(?![^\s])"), "initcall_debug"),
    (re.compile(r"(?<![^\s])systemd\.log_level=debug(?![^\s])"), "systemd.log_level=debug"),
]
# Matches systemd.log_level= with ANY value, used only to strip a stale
# differing value before re-appending the correct one -- otherwise the
# line would end up with two conflicting systemd.log_level= tokens.
STALE_LOG_LEVEL_RE = re.compile(r"(?<![^\s])systemd\.log_level=\S+")


def resolve_boot_entry(ser):
    """Determines the on-device bootloader entry file to edit for debug
    cmdline params, without hardcoding a path. Runs `bootctl status | grep
    type` -- if the device isn't a Boot Loader Specification Type #1 (i.e.
    Type #2 or unrecognized), there's no per-entry options line to edit, so
    returns None. Otherwise runs `bootctl status | grep conf` and pulls the
    current entry's real `source:` path out of that output. Returns the
    resolved path string, or None if not Type #1."""
    type_output = run_serial_command(ser, "bootctl status | grep type")
    if "Type #1" not in type_output:
        return None

    conf_output = run_serial_command(ser, "bootctl status | grep conf")
    match = BOOTCTL_SOURCE_RE.search(conf_output)
    if not match:
        raise RuntimeError(
            "bootctl status reports Boot Loader Specification Type #1, but no "
            f"'source:' line was found in its conf output to resolve the current "
            f"entry file: {conf_output!r}"
        )
    return match.group(1)


def ensure_debug_cmdline_params(ser):
    """Adds any missing debug kernel cmdline params (initcall_debug,
    log_buf_len=4M, systemd.log_level=debug) to the 'options' line of the
    device's current bootloader entry (resolved dynamically via
    resolve_boot_entry, never hardcoded), then syncs. Idempotent and
    order-independent: only params that are missing (or, for
    systemd.log_level, present with the wrong value) are added/corrected;
    params already present are left untouched. Never reverted once
    added -- intentional, so a later capture run doesn't need to redo
    this if the device wasn't reflashed in between. Returns the entry path
    that was edited, or None if the device is Boot Loader Specification
    Type #2 (or unrecognized) and has no per-entry options line to edit."""
    entry_path = resolve_boot_entry(ser)
    if entry_path is None:
        print(
            "bootctl reports Boot Loader Specification Type #2 (or unrecognized) "
            "-- no per-entry options line to edit; skipping debug kernel cmdline params."
        )
        return None

    try:
        run_serial_command(ser, f"test -f {entry_path}")
    except RuntimeError as e:
        # Checked via exit code (rc!=0), not by parsing echoed EXISTS/MISSING
        # text -- async kernel log lines (e.g. driver probe noise) can print
        # to the console with no newline before the echo, and
        # _strip_kernel_log_noise strips to the end of that physical line,
        # which can eat the echoed text itself and misreport a real file as
        # missing. rc-based detection (same mechanism run_serial_command
        # already uses for every other command) doesn't depend on parsing
        # command output at all.
        raise RuntimeError(
            f"Bootloader entry file not found on device: {entry_path}; "
            f"cannot add debug kernel cmdline params. ({e})"
        ) from e

    content = run_serial_command(ser, f"cat {entry_path}", strip_noise=False)
    lines = content.splitlines()
    options_idx = next((i for i, l in enumerate(lines) if l.startswith("options ")), None)
    if options_idx is None:
        raise RuntimeError(
            f"No 'options' line found in {entry_path}; "
            "cannot append debug kernel cmdline params."
        )

    options_line = lines[options_idx]
    to_append, already_present, corrected = [], [], []

    if LOG_BUF_LEN_RE.search(options_line):
        already_present.append(LOG_BUF_LEN_PARAM)
    else:
        to_append.append(LOG_BUF_LEN_PARAM)

    for match_re, param in EXACT_CMDLINE_PARAMS:
        if match_re.search(options_line):
            already_present.append(param)
            continue
        stale = STALE_LOG_LEVEL_RE.search(options_line) if param == "systemd.log_level=debug" else None
        if stale:
            options_line = STALE_LOG_LEVEL_RE.sub(param, options_line)
            corrected.append(f"{stale.group(0)} -> {param}")
        else:
            to_append.append(param)

    if not to_append and not corrected:
        print(f"Debug cmdline params already present in {entry_path}, skipping.")
        return entry_path

    if to_append:
        options_line = options_line.rstrip() + " " + " ".join(to_append)
    lines[options_idx] = options_line

    if to_append:
        print(f"Adding debug cmdline params: {', '.join(to_append)}")
    if corrected:
        print(f"Correcting: {', '.join(corrected)}")
    if already_present:
        print(f"Already present, left unchanged: {', '.join(already_present)}")

    new_content = "\n".join(lines) + "\n"
    write_cmd = (
        f"cat > {entry_path} << '{DEBUG_CMDLINE_HEREDOC_MARKER}'\n"
        f"{new_content}"
        f"{DEBUG_CMDLINE_HEREDOC_MARKER}\n"
        f"sync"
    )
    run_serial_command(ser, write_cmd, timeout_s=60)
    print(f"Updated {entry_path} and synced.")
    return entry_path


def remove_debug_cmdline_params(ser):
    """Strip the debug params ensure_debug_cmdline_params() added.

    The exact inverse, and the reason it exists: nothing else removes them,
    so once a capture has run the board boots with initcall_debug,
    log_buf_len=4M and systemd.log_level=debug *forever*, and
    systemd.log_level=debug measurably slows boot. A later "default" capture
    on that board is then not a default boot at all. The nightly reflashes,
    which restores a clean entry -- but that is a property of the schedule,
    not a guarantee of this script.

    Shares resolve_boot_entry() with its counterpart, so both edit the same
    file and a Type #2 bootloader is skipped identically rather than
    half-handled.
    """
    entry_path = resolve_boot_entry(ser)
    if entry_path is None:
        print("No per-entry options line to edit; skipping debug cmdline revert.")
        return None

    try:
        run_serial_command(ser, f"test -f {entry_path}")
    except RuntimeError as e:
        raise RuntimeError(
            f"Bootloader entry file not found on device: {entry_path}; "
            f"cannot remove debug kernel cmdline params. ({e})"
        ) from e

    content = run_serial_command(ser, f"cat {entry_path}", strip_noise=False)
    lines = content.splitlines()
    options_idx = next((i for i, l in enumerate(lines) if l.startswith("options ")), None)
    if options_idx is None:
        raise RuntimeError(
            f"No 'options' line found in {entry_path}; "
            "cannot remove debug kernel cmdline params."
        )

    options_line = lines[options_idx]
    removed = []
    # STALE_LOG_LEVEL_RE rather than the exact-value pattern: the goal is a
    # cmdline with no systemd.log_level at all, so a stale differing value
    # left behind by an earlier run goes too.
    for pattern in (LOG_BUF_LEN_RE, STALE_LOG_LEVEL_RE,
                    *(p for p, _token in EXACT_CMDLINE_PARAMS)):
        found = pattern.findall(options_line)
        if not found:
            continue
        removed.extend(m if isinstance(m, str) else m[0] for m in found)
        options_line = pattern.sub("", options_line)

    if not removed:
        print(f"No debug cmdline params present in {entry_path}, nothing to revert.")
        return entry_path

    # Collapse the runs of whitespace the removals left behind, so the
    # options line stays a clean single-spaced list.
    options_line = " ".join(options_line.split())
    lines[options_idx] = options_line
    print(f"Removing debug cmdline params: {', '.join(sorted(set(removed)))}")

    new_content = "\n".join(lines) + "\n"
    write_cmd = (
        f"cat > {entry_path} << '{DEBUG_CMDLINE_HEREDOC_MARKER}'\n"
        f"{new_content}"
        f"{DEBUG_CMDLINE_HEREDOC_MARKER}\n"
        f"sync"
    )
    run_serial_command(ser, write_cmd, timeout_s=60)
    print(f"Reverted {entry_path} and synced.")
    return entry_path


def ensure_collect_script(ser):
    """Writes collect_boot_logs.sh to COLLECT_SCRIPT_REMOTE_PATH if it isn't
    already there, and chmod +x's it. Safe to call every boot -- self-healing
    if /data was ever wiped, but only actually writes once per device."""
    check = run_serial_command(
        ser, f"test -f {COLLECT_SCRIPT_REMOTE_PATH} && echo EXISTS || echo MISSING"
    )
    if check.strip() == "EXISTS":
        return
    print(f"Provisioning {COLLECT_SCRIPT_REMOTE_PATH} on device ...")
    write_cmd = (
        f"cat > {COLLECT_SCRIPT_REMOTE_PATH} << '{COLLECT_SCRIPT_HEREDOC_MARKER}'\n"
        f"{COLLECT_SCRIPT_CONTENT}\n"
        f"{COLLECT_SCRIPT_HEREDOC_MARKER}\n"
        f"chmod +x {COLLECT_SCRIPT_REMOTE_PATH}"
    )
    run_serial_command(ser, write_cmd, timeout_s=60)


def collect_boot_on_device(ser, log_name: str):
    """Runs collect_boot_logs.sh (provisioning it first if needed), syncs,
    renames /data/Logs -> /data/Logs-<log_name> (e.g. "default-1",
    "debug-2") so it survives the next boot's run, and syncs again."""
    ensure_collect_script(ser)

    print(f"Running collect_boot_logs.sh (boot {log_name}) ...")
    run_serial_command(ser, f"sh {COLLECT_SCRIPT_REMOTE_PATH}", timeout_s=180)
    run_serial_command(ser, "sync")

    print(f"Renaming /data/Logs -> /data/Logs-{log_name} ...")
    run_serial_command(ser, f"rm -rf /data/Logs-{log_name}; mv /data/Logs /data/Logs-{log_name}")
    run_serial_command(ser, "sync")


def enable_adb(ser):
    print("Enabling adb ...")
    run_serial_command(ser, "touch /etc/usb-debugging-enabled")
    run_serial_command(ser, "systemctl start android-tools-adbd")


def _adb_cmd(*args, adb_serial=None) -> list:
    """Build an adb argv, routed to a specific device when one is set.

    Follows the same global-with-explicit-override idiom as TAC_PORT_NAME, so
    every existing call site keeps working untouched while the helpers stay
    unit-testable without mutating module state.
    """
    serial = adb_serial if adb_serial is not None else ADB_SERIAL
    return ["adb", *(["-s", serial] if serial else []), *args]


def wait_for_adb_device(timeout_s: int = 60, poll_interval_s: float = 2.0, *,
                        adb_serial=None):
    """Polls `adb devices` until a device shows up in the "device" (ready)
    state, as opposed to absent, "offline", or "unauthorized".

    When a serial is set, only that device counts. Previously this returned
    on the *first* ready line, so on a bench host with two boards attached it
    could report success for the wrong one and the whole capture would then
    run against someone else's device.
    """
    serial = adb_serial if adb_serial is not None else ADB_SERIAL
    deadline = time.time() + timeout_s
    seen = set()
    while True:
        # Not _adb_cmd: `adb -s X devices` still lists everything, so the
        # filtering has to happen here on the parsed output.
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) != 2:
                continue
            found_serial, state = parts
            seen.add(f"{found_serial} ({state})")
            if state != "device":
                continue
            if serial and found_serial != serial:
                continue
            return
        # Checked after the first poll, not before it: otherwise a zero or
        # already-elapsed timeout reports "No devices listed" without ever
        # having looked, which is a misleading thing to tell someone
        # debugging a cable.
        if time.time() >= deadline:
            break
        time.sleep(poll_interval_s)

    target = f" matching serial {serial}" if serial else ""
    detail = f" Saw: {', '.join(sorted(seen))}." if seen else " No devices listed."
    raise RuntimeError(
        f"No adb device{target} in 'device' state after {timeout_s}s.{detail}"
    )


def _adb_root(retries: int = 3, retry_delay_s: float = 2.0, *, adb_serial=None):
    """Runs `adb root`, retrying on transient failure. Right after adbd is
    freshly enabled (systemctl start android-tools-adbd), the device can
    show up in `adb devices` as "device" a moment before adbd is actually
    ready to accept a root-escalation request -- so the first `adb root`
    issued right after wait_for_adb_device() returns can race and fail with
    exit code 1, even though the exact same command succeeds a couple
    seconds later (confirmed live: a manual retry worked immediately)."""
    last_result = None
    for attempt in range(1, retries + 1):
        result = subprocess.run(
            _adb_cmd("root", adb_serial=adb_serial), capture_output=True, text=True
        )
        if result.returncode == 0:
            return
        last_result = result
        if attempt < retries:
            print(f"'adb root' attempt {attempt}/{retries} failed; retrying in {retry_delay_s}s ...")
            time.sleep(retry_delay_s)
    raise RuntimeError(
        f"'adb root' failed after {retries} attempts (rc={last_result.returncode}): "
        f"{last_result.stderr.strip() or last_result.stdout.strip()}"
    )


def adb_list_remote_log_names(*, adb_serial=None) -> list:
    """Discover which /data/Logs-<name> directories exist on the device.

    Lets --resume-pull work out what is actually there instead of trusting
    --num-boots to describe a capture that may have been interrupted
    part-way. Returns names sorted phase-then-index (default before debug,
    1 before 2) rather than lexically, so "default-10" does not sort between
    "default-1" and "default-2".
    """
    result = subprocess.run(
        _adb_cmd("shell", "ls", "-d", "/data/Logs-*", adb_serial=adb_serial),
        capture_output=True, text=True,
    )
    names = []
    for line in result.stdout.splitlines():
        line = line.strip().rstrip("/")
        if not line or "No such file" in line:
            continue
        base = line.rsplit("/", 1)[-1]
        if base.startswith("Logs-"):
            names.append(base[len("Logs-"):])

    phase_order = {"default": 0, "debug": 1}

    def sort_key(name):
        phase, _, index = name.rpartition("-")
        return (phase_order.get(phase, 99), phase,
                int(index) if index.isdigit() else 0)

    return sorted(set(names), key=sort_key)


def adb_pull_logs(local_dir: Path, log_names: list, *, adb_serial=None):
    """Pulls /data/Logs-<name> for every name in log_names (e.g.
    ["default-1", "default-2", "default-3", "debug-1", ...]) into
    local_dir in a single `adb pull`. `adb root` first -- /data/Logs-* is
    only readable as root on this device -- which restarts adbd, so we
    re-wait for the device to come back before pulling.

    A single pull is the fast path. If it fails, each directory is retried
    individually and only an all-failed pull raises: one missing remote
    directory used to fail the pull for all 2N boots and discard a capture
    that had already spent most of an hour booting.
    """
    local_dir.mkdir(parents=True, exist_ok=True)

    print("Restarting adb as root ...")
    _adb_root(adb_serial=adb_serial)
    wait_for_adb_device(adb_serial=adb_serial)

    remote_dirs = [f"/data/Logs-{name}" for name in log_names]
    print(f"Pulling {', '.join(remote_dirs)} -> {local_dir} ...")
    result = subprocess.run(
        _adb_cmd("pull", *remote_dirs, str(local_dir), adb_serial=adb_serial),
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return

    print(
        f"Bulk pull failed (rc={result.returncode}); retrying each directory "
        f"individually.\n{(result.stderr or result.stdout).strip()}"
    )
    pulled, failed = [], []
    for name, remote in zip(log_names, remote_dirs):
        one = subprocess.run(
            _adb_cmd("pull", remote, str(local_dir), adb_serial=adb_serial),
            capture_output=True, text=True,
        )
        if one.returncode == 0:
            pulled.append(name)
        else:
            failed.append(name)
            print(f"  {remote}: FAILED -- {(one.stderr or one.stdout).strip()}")

    if not pulled:
        raise RuntimeError(
            f"adb pull failed for every log directory ({', '.join(remote_dirs)}). "
            f"Last error: {(result.stderr or result.stdout).strip()}"
        )
    print(f"Pulled {len(pulled)} of {len(log_names)} log directories; "
          f"missing: {', '.join(failed)}")


def _read_pulled_file(local_dir: Path, log_name: str, filename: str) -> str:
    path = local_dir / f"Logs-{log_name}" / filename
    if not path.exists():
        raise RuntimeError(f"Expected pulled log file not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _read_optional_pulled_file(local_dir: Path, log_name: str, filename: str):
    """Like _read_pulled_file but returns None instead of raising.

    For files that are useful context rather than parse input. A boot must
    not be discarded because an advisory file is missing.
    """
    path = local_dir / f"Logs-{log_name}" / filename
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def cmdline_has_debug_params(cmdline):
    """True/False if a /proc/cmdline string carries the debug capture
    params, or None if the cmdline is unknown.

    The point is to be able to *prove* which cmdline a boot actually ran
    with. ensure_debug_cmdline_params() appends initcall_debug,
    log_buf_len=4M and systemd.log_level=debug to the on-device bootloader
    entry and nothing removes them, so a board stays debug-y across
    subsequent boots -- and systemd.log_level=debug measurably slows boot.
    A boot labelled "default" that in fact booted with these params is not
    comparable with the rest of the default series, and without reading the
    cmdline back there is no way to tell.

    None rather than False when there is no cmdline to inspect:
    kernel_cmdline.txt is read non-strictly, so its absence means "not
    known", and answering False there would assert the very thing this
    function exists to verify. A consumer excluding debug-polluted boots
    from a trend must be able to tell "clean" from "unverified".
    """
    if not cmdline:
        return None
    if LOG_BUF_LEN_RE.search(cmdline):
        return True
    # Reuses the same patterns ensure_debug_cmdline_params() applies, so
    # "what counts as a debug cmdline" has exactly one definition and the
    # check cannot drift away from what the script actually sets.
    return any(pattern.search(cmdline) for pattern, _token in EXACT_CMDLINE_PARAMS)


def read_pulled_logs(local_dir: Path, log_name: str) -> dict:
    """Reads the collect_boot_logs.sh outputs for one boot out of
    local_dir/Logs-<log_name>/, already pulled from the device via
    adb_pull_logs. Maps to the same text arguments build_run() needs.

    kernel_cmdline is read non-strictly and is not a build_run() input: the
    collect script has always written it (that boot's own /proc/cmdline) and
    nothing has ever read it. It is carried alongside the parse inputs so a
    boot can record which cmdline it actually ran with -- see
    cmdline_has_debug_params().
    """
    cmdline = _read_optional_pulled_file(local_dir, log_name, "kernel_cmdline.txt")
    return {
        "sat_text": _read_pulled_file(local_dir, log_name, "systemd_analyze.txt"),
        "cc_text": _read_pulled_file(local_dir, log_name, "systemd-analyze_critical_chain.txt"),
        "cc_sysinit_text": _read_pulled_file(local_dir, log_name, "systemd-analyze_critical_chain_sysinit.txt"),
        "blame_text": _read_pulled_file(local_dir, log_name, "blame_systemd.txt"),
        "dmesg_text": _read_pulled_file(local_dir, log_name, "dmesg.txt"),
        "journalctl_text": _read_pulled_file(local_dir, log_name, "journalctl.log"),
        "kernel_cmdline": (cmdline or "").strip() or None,
    }


def reboot_and_relogin(com_port: str, timeout_s: int):
    """Power-cycles the device via the Alpaca TAC (off, pause, on) and logs
    back in over serial -- same TAC primitive used for --recover. Avoids
    `systemctl reboot` over the console entirely: a hardware power cycle
    boots the device the same way every time, like the very first boot after
    a flash, rather than depending on in-OS reboot timing. Returns (ser,
    com_port_used); the COM port is re-probed from scratch if the original
    one doesn't come back (untested whether the debug-board UART bridge
    stays on the same port across a power cycle)."""
    print("Power-cycling device via Alpaca TAC (off, pause, on) ...")
    power_cycle_device()

    print(f"Waiting up to {timeout_s}s for device to come back online on {com_port} ...")
    try:
        ser, used_port = open_console(com_port, boot_timeout=timeout_s)
        return ser, used_port
    except RuntimeError as e:
        print(f"No login prompt on {com_port} ({e}); re-scanning all serial ports ...")
        ser, used_port = open_console(None, boot_timeout=timeout_s)
        return ser, used_port


def _pick_displayed(boots: list) -> list:
    """First successfully-parsed boot of each phase, in a stable order.

    Replaces `[boots[0], boots[num_boots]]`, which assumed the boots on the
    device exactly matched --num-boots and raised IndexError otherwise -- the
    --resume-pull foot-gun, and now also the case where a boot failed to
    parse and is therefore absent from the list.

    Identical output to the old indexing on a healthy 2N-boot run.
    """
    by_phase = {}
    for boot in boots:
        by_phase.setdefault(boot.get("phase"), boot)
    return [by_phase[phase] for phase in ("default", "debug") if phase in by_phase]


def pull_and_record(target: str, build_path: str, num_boots: int = 3, *,
                    adb_serial=None) -> Path:
    """Pulls the already-collected /data/Logs-default-1..N and
    /data/Logs-debug-1..N off the device (adb must already be enabled and
    the device already rebooted past the last boot's capture) and
    builds/records the JSON+HTML report. Split out from
    capture_and_record() so a run that reaches this point but then fails
    (e.g. a transient `adb root` error) can be resumed with
    `--resume-pull` instead of repeating the flash and all boots.

    num_boots=None discovers what is actually on the device instead of
    assuming, which is what makes --resume-pull safe after an interrupted
    capture.

    A boot that fails to parse is recorded as a parse_error and skipped; the
    rest are still recorded. Previously any one missing or malformed file
    propagated out of here and *nothing at all* was recorded, discarding a
    capture that had already spent the better part of an hour booting.
    """
    print("Waiting for adb device ...")
    wait_for_adb_device(adb_serial=adb_serial)

    local_dir = boot_logs_run_dir(target, build_path)
    if num_boots is None:
        log_names = adb_list_remote_log_names(adb_serial=adb_serial)
        if not log_names:
            raise RuntimeError(
                "No /data/Logs-* directories found on the device. There is "
                "nothing to resume; run a capture first."
            )
        print(f"Discovered {len(log_names)} log directory(ies) on device: "
              f"{', '.join(log_names)}")
    else:
        log_names = ([f"default-{i + 1}" for i in range(num_boots)]
                     + [f"debug-{i + 1}" for i in range(num_boots)])

    status_set(boots_expected=len(log_names))
    adb_pull_logs(local_dir, log_names, adb_serial=adb_serial)

    boots = []
    failures = []
    for log_name in log_names:
        phase, _, index = log_name.rpartition("-")
        try:
            texts = read_pulled_logs(local_dir, log_name)
            boot = build_run(
                target, build_path,
                texts["sat_text"], texts["cc_text"], texts["cc_sysinit_text"],
                texts["blame_text"], texts["dmesg_text"], texts["journalctl_text"],
            )
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            # Deliberately narrow: these are the failures a bad/missing log
            # file produces. A genuine bug still propagates as EXIT_UNKNOWN.
            message = f"{type(e).__name__}: {e}"
            print(f"WARNING: boot {log_name} could not be parsed -- {message}")
            failures.append(log_name)
            status_add_boot({
                "phase": phase, "log_name": log_name,
                "boot_index": int(index) if index.isdigit() else None,
                "parse_error": message,
            })
            continue

        boot["phase"] = phase
        boot["log_name"] = log_name
        boot["boot_index"] = int(index) if index.isdigit() else None
        # Which cmdline this boot actually ran with -- see
        # cmdline_has_debug_params() on why that is worth recording.
        boot["kernel_cmdline"] = texts.get("kernel_cmdline")
        boot["cmdline_has_debug"] = cmdline_has_debug_params(
            texts.get("kernel_cmdline")
        )
        if phase == "debug" and boot["metrics"]["kernel"].get("hitters"):
            boot["optimization_possibilities"] = (
                "Not yet analyzed — ask an agent to review the overheads above and suggest fixes."
            )
        boots.append(boot)
        status_add_boot(boot)
        txt_path = save_run_backup(target, boot)
        print(f"Backed up boot {log_name} -> {txt_path}")

    if not boots:
        # EXIT_PARSE rather than the enclosing stage's EXIT_ADB: _stage()
        # leaves an already-tagged BootbenchError alone, and the distinction
        # matters to a caller deciding whether to retry. EXIT_ADB means "the
        # logs are on the device, re-run with --resume-pull"; this failure is
        # deterministic, so that retry would fail identically an hour later.
        raise BootbenchError(
            f"No boot out of {len(log_names)} could be parsed, so there is "
            f"nothing to record. Failed: {', '.join(failures)}. The pulled "
            f"logs are kept at {local_dir} for inspection.",
            EXIT_PARSE, "parse",
        )

    # Only the first boot of each phase is recorded/rendered into the
    # JSON+HTML report (keeps the report compact); every boot is still
    # pulled, parsed, and permanently backed up above regardless of
    # num_boots.
    displayed_boots = _pick_displayed(boots)
    if not displayed_boots:
        # Reachable via --resume-pull, where the phase names come from the
        # directory names actually on the device rather than from a known
        # default/debug pattern. Previously this fell through to
        # displayed_boots[0] and surfaced as a bare IndexError traceback.
        raise BootbenchError(
            f"Parsed {len(boots)} boot(s) but none belongs to the 'default' "
            f"or 'debug' phase, so there is no column to render. Phases seen: "
            f"{sorted({b.get('phase') for b in boots})}. The pulled logs are "
            f"kept at {local_dir}.",
            EXIT_PARSE, "parse",
        )

    entry = {
        "timestamp": displayed_boots[0]["timestamp"],
        "build_path": build_path,
        "boots": displayed_boots,
    }

    data_path, html_path = _data_html_paths(target)
    data = load_data(data_path)
    if not data.get("runs"):
        data["device"] = target
    add_run(data, entry)
    save_data(data, data_path)
    render(data_path, html_path)
    status_output(data_json=data_path, report_html=html_path,
                  boot_logs_dir=local_dir)

    print(f"\nRecorded entry '{entry['timestamp']}' with {len(displayed_boots)} boot(s) displayed "
          f"({len(boots)} captured total, {len(data['runs'])} total entries) -> {data_path}")
    print(f"Report regenerated -> {html_path}")
    if failures:
        print(f"PARTIAL: {len(failures)} of {len(log_names)} boot(s) failed to "
              f"parse and were skipped: {', '.join(failures)}")

    return html_path


def capture_and_record(target: str, build_path: str,
                        com_port: str = None, boot_timeout: int = 480,
                        num_boots: int = 3, *, adb_serial=None,
                        revert_debug_cmdline: bool = False) -> Path:
    print("Logging in over serial console" + (f" on {com_port}" if com_port else " (auto-detecting port)") + " ...")
    ser, com_port = open_console(com_port, boot_timeout=boot_timeout)
    print(f"Logged in via {com_port}")

    if target is None:
        target = detect_target_via_serial(ser)
        print(f"Detected target: {target} (via {com_port})")

    boot_units = [("default", i + 1) for i in range(num_boots)] + \
                 [("debug", i + 1) for i in range(num_boots)]

    for idx, (phase, n) in enumerate(boot_units):
        # Between boots is a safe place to stop: nothing is mid-write. The
        # flash is already finished by the time capture runs, so honoring a
        # cancel here can't interrupt a partition write.
        raise_if_cancelled(f"before boot {phase}-{n}")
        if idx > 0:
            if phase == "debug" and n == 1:
                print("Adding debug kernel cmdline params ...")
                ensure_debug_cmdline_params(ser)
            ser.close()
            ser, com_port = reboot_and_relogin(com_port, boot_timeout)
        log_name = f"{phase}-{n}"
        print(f"\n--- Boot {log_name} ({idx + 1}/{len(boot_units)}) ---")
        collect_boot_on_device(ser, log_name)

    if revert_debug_cmdline:
        # Leaves the board on a clean default cmdline for whoever uses it
        # next. Off by default: the nightly reflashes anyway, and a hand-run
        # capture should not silently change device state it didn't set up.
        print("Reverting debug kernel cmdline params ...")
        remove_debug_cmdline_params(ser)

    enable_adb(ser)
    ser.close()

    return pull_and_record(target, build_path, num_boots, adb_serial=adb_serial)


# =============================================================================
# CLI -- stage words (flash / capture / report / all), combinable in one
# invocation. Stages always run in pipeline order (flash -> capture ->
# report) regardless of the order they're typed in. When flash runs in the
# same invocation as capture, capture reuses the target/build-path/com-port
# flash just resolved instead of re-detecting them. capture already writes
# and renders the JSON/HTML report as part of pulling logs, so a standalone
# 'report' stage is only meaningful when 'capture' is NOT also requested.
# =============================================================================

STAGE_ORDER = ["flash", "capture", "report"]


def parse_stages(raw_stages):
    """Expands 'all' to flash+capture+report, dedupes, and returns stages in
    fixed pipeline order regardless of how they were typed on the CLI."""
    requested = set()
    for s in raw_stages:
        requested.update(STAGE_ORDER if s == "all" else [s])
    return [s for s in STAGE_ORDER if s in requested]


def cmd_flash(args):
    """Runs build-discovery -> EDL entry -> confirm -> PCAT flash. Returns a
    dict of {target, build_path, com_port} for capture to reuse when it runs
    in the same invocation, or None if the pipeline should stop here
    (--recover, --dry-run, or the user declined the confirmation prompt)."""
    global TAC_PORT_NAME
    TAC_PORT_NAME = args.tac_port

    if args.recover:
        print("Recovering device: power-cycling via TAC...")
        with _stage("tac", EXIT_TAC):
            power_cycle_device()
        print("Done. Device should re-boot normally.")
        return None

    print(f"Finding latest nightly build under {YOCTO_SHARE} ...")
    with _stage("build_discovery", EXIT_BUILD_DISCOVERY):
        build_number, build_folder = find_latest_build_name(YOCTO_SHARE)
        latest_build = Path(YOCTO_SHARE) / build_folder
    print(f"Latest build: {latest_build.name}")
    status_set(share_root=YOCTO_SHARE, build_number=build_number,
               build_folder=build_folder)

    used_com_port = args.com_port
    target = args.target
    memory_type = args.memory_type

    if target:
        print(f"Using target override: {target}")
    if memory_type:
        print(f"Using memory type override: {memory_type}")

    # The device has to be booted to answer either question, and it is only
    # booted *before* EDL entry -- hence one console session here, serving
    # both. Two cases cannot probe: --skip-edl means the board is already in
    # EDL with no shell left to ask, and --dry-run promises not to touch the
    # device at all.
    if memory_type is None and args.skip_edl:
        memory_type = MEMORY_TYPE_DEFAULT
        print(f"--skip-edl: the device is already in EDL, so its storage "
              f"cannot be probed. Assuming -MEMORYTYPE {memory_type}; pass "
              f"--memory-type if that is wrong.")
    probe_storage = memory_type is None and not args.dry_run

    if target is None or probe_storage:
        print("Power-cycling device via Alpaca TAC before probing it (known-good boot state) ...")
        with _stage("tac", EXIT_TAC):
            power_cycle_device()

        print("Logging in over serial console" + (f" on {args.com_port}" if args.com_port else " (auto-detecting port)") + " ...")
        with _stage("serial_login", EXIT_SERIAL_LOGIN):
            ser, used_com_port = open_console(args.com_port, boot_timeout=args.boot_timeout)
            try:
                if target is None:
                    target = detect_target_via_serial(ser)
                    print(f"Detected target: {target} (via {used_com_port})")
                if probe_storage:
                    with _stage("storage_detect", EXIT_SERIAL_LOGIN):
                        storage = detect_storage_type(ser)
                    memory_type = storage["memory_type"]
                    status_set(storage_disk=storage["disk"])
                    print(f"Detected storage: {memory_type} "
                          f"(/dev/{storage['disk']} -- {storage['source']})")
            finally:
                ser.close()
    status_set(target=target, memory_type=memory_type)

    with _stage("build_discovery", EXIT_BUILD_DISCOVERY):
        build_dir = resolve_build_dir(latest_build, target)
    print(f"Resolved build directory: {build_dir}")

    if args.dry_run:
        print("\n--dry-run: stopping before touching the device.")
        print(f"Would run: {PCAT_EXE} -PLUGIN SD -DEVICE <discovered-after-edl> "
              f'-BUILD "{build_dir}" '
              f"-MEMORYTYPE {memory_type or '<read off the device with lsblk>'} "
              f"-SLOT 0")
        return None

    # Last safe point: after this the device goes into EDL and PCAT starts
    # writing partitions. A cancel arriving later is refused by design.
    raise_if_cancelled("before entering EDL mode")

    with _stage("edl", EXIT_EDL):
        enter_edl_mode() if not args.skip_edl else print("Skipping EDL trigger; assuming device is already in EDL mode.")

        print("Waiting for device to re-enumerate in EDL mode ...")
        time.sleep(5)
        edl_device = wait_for_edl_device()
        device_id = pcat_device_id(edl_device)
    print(f"Found EDL device: {edl_device}")

    print("\nAbout to flash:")
    print(f"  Device ID:    {device_id}")
    print(f"  Build dir:    {build_dir}")
    print(f"  Memory type:  {memory_type}")
    print(f"  Slot:         0")

    if not args.yes:
        reply = prompt_yes_no("\nProceed with flashing? [y/N] ")
        if not reply:
            print("Aborted.")
            return None

    # Not cancellable: PCAT is writing boot partitions, and interrupting that
    # leaves the device unbootable. The agent refuses a mid-flash cancel for
    # the same reason.
    with _stage("flash", EXIT_FLASH):
        run_pcat_flash(device_id, build_dir, memory_type)
    print("\nFlash completed successfully.")

    build_path = str(latest_build / PERFORMANCE_SUBDIR)
    status_set(build_path=build_path)

    return {
        "target": target,
        "build_path": build_path,
        "com_port": used_com_port,
    }


def cmd_capture(args, handoff=None):
    """Runs (or resumes) boot-time capture. Uses target/build_path/com_port
    from `handoff` (set when 'flash' ran in the same invocation) in
    preference to --target/--build-path/--com-port."""
    if args.resume_pull:
        if not args.target:
            raise BootbenchError(
                "--resume-pull requires --target (no serial console is opened "
                "to auto-detect it).", EXIT_USAGE, "usage")
        if not args.build_path:
            raise BootbenchError(
                "--resume-pull requires --build-path.", EXIT_USAGE, "usage")
        print("--resume-pull: skipping login and the boot loop; pulling already-collected logs ...")
        status_set(target=args.target, build_path=args.build_path,
                   build_number=parse_build_number(args.build_path))
        with _stage("adb", EXIT_ADB):
            with report_lock(args.lock_file):
                pull_and_record(args.target, args.build_path, args.num_boots,
                                adb_serial=args.adb_serial)
        return

    target = handoff["target"] if handoff else args.target
    build_path = handoff["build_path"] if handoff else args.build_path
    com_port = handoff["com_port"] if handoff else args.com_port

    if not build_path:
        raise BootbenchError(
            "capture (without flash) requires --build-path.", EXIT_USAGE, "usage")

    status_set(target=target, build_path=build_path,
               build_number=parse_build_number(build_path))

    print("\nCapturing boot-time data over serial console...")
    with _stage("boot_collect", EXIT_BOOT_COLLECT):
        with report_lock(args.lock_file):
            capture_and_record(
                target=target,
                build_path=build_path,
                com_port=com_port,
                boot_timeout=args.boot_timeout,
                num_boots=args.num_boots,
                adb_serial=args.adb_serial,
                revert_debug_cmdline=args.revert_debug_cmdline,
            )


def cmd_report(args, handoff=None):
    """Standalone report maintenance: (optionally) append a run JSON, then
    re-render the HTML from the JSON. No device involved. Only reached when
    'capture' isn't also requested in this invocation, since capture already
    records and renders its own data as part of collecting it."""
    target = (handoff["target"] if handoff else None) or args.target
    if not target:
        raise BootbenchError(
            "the 'report' stage requires --target (or run together with "
            "'flash' to auto-detect it).", EXIT_USAGE, "usage")

    data_path, html_path = _data_html_paths(target)
    with _stage("record", EXIT_RECORD):
        with report_lock(args.lock_file):
            if args.report_cmd == "add-run":
                if not args.run_json:
                    raise BootbenchError(
                        "--report-cmd add-run requires --run-json <path|->",
                        EXIT_USAGE, "usage")
                raw = sys.stdin.read() if args.run_json == "-" else Path(args.run_json).read_text(encoding="utf-8")
                run = json.loads(raw)
                data = load_data(data_path)
                add_run(data, run)
                save_data(data, data_path)
                print(f"Appended run -> {data_path}")

            render(data_path, html_path)
    print(f"Report regenerated -> {html_path}")
    status_output(data_json=str(data_path), report_html=str(html_path))


def cmd_latest_build(args):
    """Report the newest nightly build on the share. Touches no hardware.

    Dispatched before parse_stages() because this is not a stage in the
    flash -> capture -> report pipeline: it is a standalone query, and the
    scheduler calls it every tick to decide whether a run is even worth
    starting. With --json it prints a single JSON object and nothing else,
    so the caller can parse stdout without filtering prose out of it."""
    with _stage("build_discovery", EXIT_BUILD_DISCOVERY):
        info = discover_latest_build(YOCTO_SHARE, args.target)

    if args.json:
        print(json.dumps(info, indent=2))
        return

    print(f"Share root:   {info['share_root']}")
    print(f"Build folder: {info['build_folder']}")
    print(f"Build number: {info['build_number']}")
    print(f"Build path:   {info['build_path']}")
    if info["target"]:
        ready = "yes" if info["target_image_ready"] else "NO"
        print(f"Target:       {info['target']}")
        print(f"Image ready:  {ready} ({info['build_dir']})")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="bootbench.py", description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "stage", nargs="+",
        choices=["flash", "capture", "report", "all", "latest-build"],
        help="One or more stages to run, in any order (always executed as "
             "flash -> capture -> report). 'all' is shorthand for all three. "
             "'latest-build' is standalone: it reports the newest nightly on "
             "the share and touches no hardware.",
    )

    common = parser.add_argument_group("common")
    common.add_argument("--target", help="Override/declare target name (e.g. iq-9075-evk). Required for 'report' or 'capture --resume-pull' when run standalone; auto-detected via serial console by 'flash' or plain 'capture' otherwise.")
    common.add_argument("--com-port", help="Serial console COM port, used for login/detection (auto-detected by probing all ports if omitted)")
    common.add_argument("--boot-timeout", type=int, default=480, help="Seconds to wait for a login prompt, on first login and after each reboot (default: 480)")

    flash_group = parser.add_argument_group("flash")
    flash_group.add_argument("--tac-port", help="Alpaca TAC COM port name, e.g. VTP8 (auto-detected if only one TAC device)")
    flash_group.add_argument("--dry-run", action="store_true", help="Resolve paths and print the plan, then exit before touching the device (stops before EDL mode)")
    flash_group.add_argument("--yes", action="store_true", help="Skip the confirmation prompt before flashing (otherwise waits for manual y/N after entering EDL mode)")
    flash_group.add_argument("--skip-edl", action="store_true", help="Device is already in EDL mode; skip the TAC BootToEDL step")
    flash_group.add_argument("--recover", action="store_true", help="Power-cycle the device via TAC (out of EDL/Sahara back to normal boot) and exit -- use with the 'flash' stage")

    capture_group = parser.add_argument_group("capture")
    capture_group.add_argument("--build-path", help=r'Nightly build path this boot used, e.g. "\\swayam\...\performance" (required if capture runs without flash in the same command; supplied automatically otherwise)')
    capture_group.add_argument("--num-boots", type=int, default=None, help="Number of consecutive boots to capture PER PHASE -- e.g. 3 (the default) captures 3 default-cmdline boots then 3 debug-cmdline boots (6 total). Values < 1 fall back to 3. With --resume-pull, omitting it discovers the boots already on the device.")
    capture_group.add_argument("--resume-pull", action="store_true", help="Skip login/boot-loop entirely: adb is already enabled and all boots' /data/Logs-<n> are already on the device (a prior run reached adb pull and failed partway) -- just pull logs and (re)build the report. Requires --target and --build-path.")

    report_group = parser.add_argument_group("report")
    report_group.add_argument("--report-cmd", choices=["render", "add-run"], default="render", help="'render' (default) just regenerates the HTML from the existing JSON; 'add-run' appends --run-json first")
    report_group.add_argument("--run-json", help="Path to a run JSON to append (or '-' for stdin), used with --report-cmd add-run")

    # Flags for running this script from something other than a keyboard. All
    # additive and all optional: every invocation documented in SKILL.md
    # behaves identically without them.
    auto = parser.add_argument_group(
        "automation",
        "For unattended/scheduled runs. Omit these for hand-run invocations.")
    auto.add_argument("--json", action="store_true", help="With 'latest-build', print ONLY a JSON object to stdout (no prose)")
    auto.add_argument("--json-status", metavar="PATH", help="Write a machine-readable status document to PATH. Written from a finally block, so it exists on success, on failure, and on Ctrl-C.")
    auto.add_argument("--adb-serial", help="adb device serial to target, i.e. 'adb -s <serial>'. Required when more than one device is attached to this host.")
    auto.add_argument("--boot-charts-dir", metavar="DIR", help=f"Override where the report JSON/HTML are written (default: {BOOT_CHARTS_DIR})")
    auto.add_argument("--boot-logs-dir", metavar="DIR", help=f"Override where pulled boot logs are stored (default: {BOOT_LOGS_DIR})")
    auto.add_argument("--share-root", metavar="PATH", help=f"Override the nightly build share (default: {YOCTO_SHARE})")
    auto.add_argument("--non-interactive", action="store_true", help="Never prompt. A prompt that would have been shown becomes a usage error instead of blocking (or raising EOFError on a closed stdin).")
    auto.add_argument("--lock-file", metavar="PATH", help="Hold an exclusive lock at PATH while reading/writing the report, so a hand-run invocation and a scheduled one cannot interleave and lose a run.")
    auto.add_argument("--cancel-file", metavar="PATH", help="Poll PATH between boots and before flashing; if it exists, stop cleanly (exit 130). A cancel arriving mid-flash is refused by design.")
    auto.add_argument("--revert-debug-cmdline", action="store_true", help="Remove initcall_debug/log_buf_len/systemd.log_level=debug from the bootloader entry after the debug phase, so the device is not left booting debug-y forever.")
    auto.add_argument("--memory-type", choices=MEMORY_TYPES, help=f"PCAT -MEMORYTYPE. Omit it and the storage class is read off the device with 'lsblk' over the serial console before EDL entry (default if it cannot be probed: {MEMORY_TYPE_DEFAULT}).")

    return parser


def _run(args, parser) -> int:
    """The whole pipeline, returning an exit code. Separated from main() so
    that main() can own exactly one thing: writing the status document no
    matter how this returns or raises."""
    global ADB_SERIAL, NON_INTERACTIVE, CANCEL_FILE

    # First, before anything reads these globals.
    apply_path_overrides(args)
    ADB_SERIAL = args.adb_serial
    NON_INTERACTIVE = args.non_interactive or not sys.stdin.isatty()
    CANCEL_FILE = args.cancel_file

    if "latest-build" in args.stage:
        if len(args.stage) > 1:
            parser.error("'latest-build' is standalone and cannot be combined "
                         "with other stages")
        cmd_latest_build(args)
        return EXIT_OK

    stages = parse_stages(args.stage)
    status_set(stages_requested=list(stages))

    if args.num_boots is not None and args.num_boots < 1:
        print(f"--num-boots must be >= 1 (got {args.num_boots}); defaulting to 3.")
        args.num_boots = 3
    if args.num_boots is None and not args.resume_pull:
        # Only --resume-pull can discover the count from the device; every
        # other path has to decide how many boots to perform up front, so
        # None there would be a bug rather than a request to auto-detect.
        args.num_boots = 3

    if args.recover and "flash" not in stages:
        parser.error("--recover requires the 'flash' stage")
    if "capture" in stages and "flash" not in stages and not args.resume_pull and not args.build_path:
        parser.error("capture without flash requires --build-path")
    if "report" in stages and "capture" not in stages and "flash" not in stages and not args.target:
        parser.error("the 'report' stage requires --target (unless run together with 'flash')")

    ran = []
    handoff = None
    if "flash" in stages:
        handoff = cmd_flash(args)
        ran.append("flash")
        status_set(stages_run=list(ran))
        if handoff is None:
            return EXIT_OK  # --recover, --dry-run, or the user declined

    if "capture" in stages:
        cmd_capture(args, handoff)
        ran.append("capture")
        status_set(stages_run=list(ran))

    if "report" in stages:
        if "capture" in stages:
            # Previously this branch silently did nothing, which reads as a
            # dropped stage when you are looking at a log rather than a
            # terminal. Capture already recorded and rendered.
            print("\nreport: already recorded and rendered by the capture "
                  "stage; nothing to do.")
        else:
            cmd_report(args, handoff)
            ran.append("report")
            status_set(stages_run=list(ran))

    return EXIT_OK


def main():
    parser = build_parser()
    args = parser.parse_args()
    # Before the try, so a failure inside _run() -- including one in
    # apply_path_overrides() on its first line -- still has a populated
    # document to be recorded into.
    status_init(args)

    exit_code = EXIT_UNKNOWN
    try:
        exit_code = _run(args, parser)
    except BootbenchError as e:
        # CancelledError arrives here too, and is handled identically: the
        # message a human sees is the one the underlying RuntimeError
        # carried, and only the exit code is more specific than before.
        print(f"\n{e}")
        exit_code = e.exit_code
        status_set(error_class=type(e).__name__, error_message=str(e),
                   failure_stage=e.stage)
    except KeyboardInterrupt:
        print("\nAborted by user.")
        exit_code = EXIT_INTERRUPTED
        status_set(error_class="KeyboardInterrupt",
                   error_message="aborted by user")
    except SystemExit as e:
        # argparse's parser.error() raises this, and SystemExit is not an
        # Exception -- so without this clause a usage error would be recorded
        # as exit 1 while the process actually exited 2.
        exit_code = e.code if isinstance(e.code, int) else EXIT_USAGE
        if exit_code:
            status_set(error_class="SystemExit", failure_stage="usage",
                       error_message=f"exited {exit_code} during argument handling")
    except Exception as e:
        # Not a known failure mode -- i.e. a bug. Keep the traceback, since
        # swallowing it would make this strictly harder to debug than it was
        # before any of this machinery existed.
        traceback.print_exc()
        exit_code = EXIT_UNKNOWN
        status_set(error_class=type(e).__name__, error_message=str(e))
    finally:
        status_set(exit_code=exit_code, ended_utc=_utcnow())
        write_status(getattr(args, "json_status", None))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
