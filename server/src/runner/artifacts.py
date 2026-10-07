"""Classifying, selecting, and mirroring a job's artifacts.

Six boots produce roughly twelve files each -- including a decompressed
`proc_config.txt` and a `Boot_Trace.txt` -- so "fetch everything" is tens of
megabytes per device per night for data nobody reads unless they are already
debugging. The manifest carries a `kind` per entry and this module decides
which kinds are worth the transfer.

Two rules that are easy to get wrong and expensive to get wrong:

  * Artifacts are fetched even when the job FAILED. A flash failure's
    runner.log and status document are precisely what you want at 9 a.m.;
    fetching only on success would discard the evidence for every outcome
    worth investigating.

  * `relpath` comes from the bench host and names a file on this one. It is
    validated against the manifest and rejected if it escapes the
    destination, because "the agent is trusted" is a property of today's
    deployment, not of the code.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from ..utils.logger import get_logger

log = get_logger("artifacts")

# The seven files per boot that the skill's own parsers read. Spelled here
# rather than imported from the agent: `client/` must not be importable from
# `server/` -- it is deployed standalone to a machine that has only the
# skill's dependencies. tests/test_agent.py asserts the two lists agree.
PARSE_FILES = frozenset({
    "systemd_analyze.txt", "systemd-analyze_critical_chain.txt",
    "systemd-analyze_critical_chain_sysinit.txt", "blame_systemd.txt",
    "dmesg.txt", "journalctl.log", "kernel_cmdline.txt",
})

# Always fetched. `status` is not negotiable -- ingestion is defined in terms
# of it -- and the rest are small and are what the dashboard renders.
ESSENTIAL_KINDS = frozenset({
    "status", "runner_log", "chart_json", "chart_html",
    "boot_txt", "boot_parse",
})

# Fetched only with fetch_full_logs: plot_systemd.svg, proc_config.txt,
# Boot_Trace.txt, lsmod.txt. Useful when chasing a specific regression,
# wasteful nightly.
OPTIONAL_KINDS = frozenset({"boot_extra"})

ALL_KINDS = ESSENTIAL_KINDS | OPTIONAL_KINDS


def classify(relpath: str) -> str:
    """Bucket one artifact by filename.

    Mirrors `client/agent.py::_classify`, whose rule is "am I inside a
    per-boot `Logs-*` folder?": inside, the seven files the parsers read are
    `boot_parse` and everything else is a large extra; outside, a `.txt` is
    one of `save_run_backup`'s human-readable per-boot backups. The agent
    sends a `kind` of its own and the server normally trusts it; this exists
    so the fake runner, the tests, and a manifest from an older agent that
    predates a kind all agree on the same rule.
    """
    pure = PurePosixPath(relpath)
    name = pure.name
    if name == "status.json":
        return "status"
    if name == "runner.log":
        return "runner_log"
    if name.startswith("bootchart-data-") and name.endswith(".json"):
        return "chart_json"
    if name.startswith("bootchart-overview-") and name.endswith(".html"):
        return "chart_html"

    if pure.parent.name.startswith("Logs-"):
        return "boot_parse" if name in PARSE_FILES else "boot_extra"
    return "boot_txt" if name.endswith(".txt") else "boot_extra"


def wanted_kinds(fetch_full_logs=False) -> frozenset:
    return ALL_KINDS if fetch_full_logs else ESSENTIAL_KINDS


def select(entries, fetch_full_logs=False) -> list:
    """The subset of a manifest worth downloading, smallest first.

    Smallest first is not cosmetic: if the connection dies partway through,
    the files that matter for ingestion and for the dashboard are already
    here and the ones left behind are the bulky optional ones.
    """
    wanted = wanted_kinds(fetch_full_logs)
    chosen = [
        entry for entry in entries
        if (entry.get("kind") or classify(entry.get("relpath", "")))
        in wanted
    ]
    return sorted(chosen, key=lambda e: (e.get("size") or 0,
                                         e.get("relpath") or ""))


def safe_destination(dest: Path, relpath: str) -> Path:
    """Resolve `relpath` under `dest`, or raise.

    `..`, an absolute path, and a Windows drive letter are all rejected
    before any bytes are written, not after.
    """
    pure = PurePosixPath(str(relpath).replace("\\", "/"))
    parts = pure.parts
    if not parts:
        raise ValueError("artifact path is empty")
    if pure.is_absolute() or ":" in parts[0]:
        raise ValueError(f"artifact path is not relative: {relpath!r}")
    if any(part in ("..", "") for part in parts):
        raise ValueError(f"artifact path escapes the destination: {relpath!r}")

    dest = Path(dest).resolve()
    target = (dest / Path(*pure.parts)).resolve()
    if dest != target and dest not in target.parents:
        raise ValueError(f"artifact path escapes the destination: {relpath!r}")
    return target


def mirror(runner, job_id, entries, dest) -> dict:
    """Download `entries` into `dest`, verifying each checksum.

    A checksum mismatch or a failed fetch is logged and skipped rather than
    raised. The run has already happened; losing one `.txt` backup must not
    cost the database the status document sitting next to it. The returned
    summary names everything that went wrong so the caller can log it
    against the run.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    written, skipped, bad = [], [], []
    total = 0
    for entry in entries:
        relpath = entry.get("relpath") or ""
        try:
            target = safe_destination(dest, relpath)
        except ValueError as exc:
            log.warning("job %s: refusing artifact %r: %s", job_id, relpath, exc)
            skipped.append(relpath)
            continue

        try:
            data = runner.fetch_artifact(job_id, relpath)
        except Exception as exc:  # noqa: BLE001 - any transport failure
            log.warning("job %s: cannot fetch %s: %s", job_id, relpath, exc)
            skipped.append(relpath)
            continue

        expected = entry.get("sha256")
        if expected:
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                # Not fatal, but it means the file on disk here is not the
                # file the bench host measured, and anything parsed from it
                # would be a plausible-looking wrong number.
                log.warning("job %s: checksum mismatch for %s "
                            "(manifest %s, got %s)",
                            job_id, relpath, expected[:12], actual[:12])
                bad.append(relpath)
                continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(relpath)
        total += len(data)

    return {
        "written": written, "skipped": skipped, "corrupt": bad,
        "bytes": total, "dest": str(dest),
    }


def find(dest, name) -> Path | None:
    """The first mirrored file with this basename, or None.

    Used to recover `status.json` from the downloaded tree when the agent's
    job record did not carry the parsed document -- the case when the agent
    was restarted mid-job and finalized the run from the file.
    """
    dest = Path(dest)
    if not dest.is_dir():
        return None
    for path in sorted(dest.rglob(name)):
        if path.is_file():
            return path
    return None
