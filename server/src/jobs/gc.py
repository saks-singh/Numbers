"""Retention. Nothing else deletes anything.

Three stores grow without bound and each needs its own rule:

  * `run` rows and their `boot` children -- cheap, but the dashboard gets
    slower and `boot_trend` wider than anyone reads.
  * the coordinator's mirrored artifact trees under `artifacts/runs/<id>/`
    -- the expensive one. Six boots of parse logs plus a trace dump is tens
    of megabytes a night per device.
  * `scheduler_decision` rows -- four an hour per device, forever. Kept
    longer than runs, because "why didn't it run three months ago?" is a
    question people genuinely ask, and a decision row is 100 bytes.

Two bounds rather than one, both applied: an age (`keep_days`) and a count
(`keep_runs_per_device`). Age alone lets a device that runs every fifteen
minutes during a debugging session bloat the table; count alone means a
device nobody has touched in a year keeps its whole history while an active
one loses last week.

What is never deleted:

  * anything `queued` or `running` -- deleting a row the worker is holding
    would orphan a real job on a bench host.
  * `scheduler_decision.run_id` survives its run: the FK is
    `ON DELETE SET NULL`, so pruning a run leaves the audit trail intact
    with a null pointer rather than cascading the decision away.

Deliberately not run from the worker or the tick. It is a `main.py gc`
subcommand so it appears in a crontab line someone can read, and so a
mistake is one `--dry-run` away from being visible instead of running
silently every fifteen minutes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..db import pool, queries
from ..utils.logger import get_logger

log = get_logger("gc")


def _artifact_dir(settings, run) -> Path:
    """Where this run's mirror lives.

    Prefers the path the worker recorded, because `artifact_root` may have
    moved since. Falls back to recomputing it, which covers rows that
    failed before the worker wrote the column.
    """
    recorded = run.get("artifact_dir")
    if recorded:
        return Path(recorded)
    return settings.run_dir(run["run_id"])


def _is_inside(path, root) -> bool:
    """Whether `path` is under `root`, without touching the filesystem.

    Guards the one genuinely dangerous line in this module: a stale or
    hand-edited `artifact_dir` must not be able to talk `shutil.rmtree`
    into walking somewhere else.
    """
    try:
        path = Path(path).resolve()
        root = Path(root).resolve()
    except OSError:
        return False
    return path != root and root in path.parents


def collect(settings, *, keep_days=None, keep_per_device=None) -> list:
    """The runs retention would remove, newest-last. Reads only."""
    keep_days = settings.keep_days if keep_days is None else keep_days
    keep_per_device = (settings.keep_runs_per_device
                       if keep_per_device is None else keep_per_device)
    with pool.connection() as conn:
        return queries.runs_to_prune(conn, keep_days=keep_days,
                                     keep_per_device=keep_per_device)


def sweep(settings, *, keep_days=None, keep_per_device=None,
          decision_keep_days=None, dry_run=False) -> dict:
    """Delete aged runs, their artifact mirrors, and old decision rows.

    Order matters: the directory goes first, then the row. A crash between
    the two leaves a row whose artifacts are gone -- the run page degrades
    to "artifacts pruned", which is honest -- whereas the reverse leaves a
    directory nothing will ever reference again.
    """
    keep_days = settings.keep_days if keep_days is None else keep_days
    keep_per_device = (settings.keep_runs_per_device
                       if keep_per_device is None else keep_per_device)
    if decision_keep_days is None:
        # Decisions are tiny and are the only record of why a run does not
        # exist, so they outlive the runs by a wide margin.
        decision_keep_days = max(int(keep_days) * 2, 365)

    summary = {
        "runs": [], "bytes": 0, "dirs": 0, "decisions": 0,
        "dry_run": bool(dry_run),
        "keep_days": int(keep_days), "keep_per_device": int(keep_per_device),
    }

    with pool.connection() as conn:
        doomed = queries.runs_to_prune(conn, keep_days=keep_days,
                                       keep_per_device=keep_per_device)
        root = settings.artifact_path

        removed = []
        for run in doomed:
            directory = _artifact_dir(settings, run)
            size = _directory_size(directory)
            summary["bytes"] += size
            summary["runs"].append({
                "run_id": run["run_id"], "device_id": run["device_id"],
                "build_number": run.get("build_number"),
                "status": run.get("status"), "bytes": size,
                "artifact_dir": str(directory),
            })
            if dry_run:
                removed.append(run["run_id"])
                continue

            if directory.exists():
                if not _is_inside(directory, root):
                    # Refuse rather than guess. A run whose artifact_dir
                    # points outside artifact_root is a configuration
                    # mistake, and the row stays so it stays visible.
                    log.warning("run %s: artifact_dir %s is outside %s; "
                                "not deleting", run["run_id"], directory, root)
                    continue
                try:
                    shutil.rmtree(directory)
                    summary["dirs"] += 1
                except OSError as exc:
                    log.warning("run %s: cannot remove %s: %s",
                                run["run_id"], directory, exc)
                    continue
            removed.append(run["run_id"])

        if not dry_run:
            if removed:
                queries.delete_runs(conn, removed)
            summary["decisions"] = queries.delete_decisions_before(
                conn, decision_keep_days)
            conn.commit()

        summary["deleted"] = len(removed)

    log.info("gc: %s run(s), %s dir(s), %.1f MB, %s decision row(s)%s",
             summary["deleted"], summary["dirs"],
             summary["bytes"] / 1_048_576, summary["decisions"],
             " (dry run)" if dry_run else "")
    return summary


def _directory_size(directory) -> int:
    total = 0
    try:
        for path in Path(directory).rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total
