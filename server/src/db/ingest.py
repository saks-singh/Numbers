"""Turn a finished job into rows.

The `--json-status` document is the authoritative source, and the
alternatives lose for structural reasons rather than aesthetic ones:

  * The skill's own per-target JSON **cannot** answer the question. It
    records 2 of the 2N boots, and `add_run` does `del runs[:-MAX_RUNS]`
    with MAX_RUNS = 30. "Every boot of every run, for the last year" is not
    a query you can run against it at any price.
  * The `.txt` backups are a human format with no schema. Parsing them
    means a second parser to keep in step with the first.
  * stdout is prose interleaved with PCAT's own output.
  * Re-parsing the raw logs here is genuinely attractive -- the parsers are
    pure and import fine on Linux -- but it redoes work the bench host
    already did, and the two can silently disagree. It is kept as the
    *repair* path (`reparse_boot_logs`), not the primary one.

Three entry points, in descending order of how often they run:

    ingest_status          a job the worker just watched finish
    ingest_from_target_json  history that predates --json-status
    reparse_boot_logs      repair one run from its pulled log files

All three are idempotent, and all three go through `queries.upsert_boot` on
(run_id, log_name) to get that. It is not defensiveness: a run re-adopted
after an agent restart is ingested twice by design.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..utils import bootbench_api as bb
from . import queries, schema

log = logging.getLogger(__name__)

# The status document version this module understands. A document from the
# future is ingested anyway -- the fields we read are additive and a refusal
# would lose a night's data over a field we do not use -- but it is logged,
# because the alternative is a silent partial ingest.
SUPPORTED_SCHEMA_VERSION = 1


class IngestError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# coercion
# ---------------------------------------------------------------------------

def _sec(value):
    """Anything the skill might have written -> a 3-decimal float or None.

    Numbers pass through; strings go to the skill's own `parse_seconds`, so
    "12.481 s" is interpreted by the same code that produced it. Delegating
    rather than re-implementing matters here more than it usually does: a
    second regex that is subtly wrong produces a plausible number, and a
    plausible wrong number on a trend chart is worse than a crash.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 3)
    parsed = bb.parse_seconds(str(value))
    return None if parsed is None else round(float(parsed), 3)


def _utc(value):
    """An ISO-8601 string from the status doc -> an aware datetime.

    Returns None rather than raising: a missing or malformed timestamp costs
    an ordering hint, and `queued_at` still orders the run. Losing the whole
    night's boot times over it would be a bad trade.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        log.warning("unparseable timestamp %r", value)
        return None
    # `write_status` writes aware UTC, but a hand-edited or backfilled
    # document may not. Assume UTC rather than the server's zone: the
    # document is written on a Windows host in another timezone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _stages(value):
    """'all' or 'flash capture' or ['flash'] -> a TEXT[]-ready list."""
    if value is None:
        return []
    if isinstance(value, str):
        return value.replace(",", " ").split()
    return [str(v) for v in value]


def boot_index_from_log_name(log_name):
    """'default-2' -> 2, and None for a name without a numeric suffix.

    Needed because `build_run` does not set `boot_index` -- `pull_and_record`
    attaches it afterwards -- so the skill's own per-target JSON, which is
    what backfill reads, does not carry it. Without this every backfilled
    boot would land with a NULL boot_index and be invisible to `trend()`,
    which pins boot_index = 1. The history would load and chart nothing.
    """
    if not log_name:
        return None
    _, _, tail = str(log_name).rpartition("-")
    return int(tail) if tail.isdigit() else None


def phase_from_log_name(log_name):
    """'default-2' -> 'default'."""
    if not log_name:
        return None
    head, sep, _ = str(log_name).rpartition("-")
    return head if sep else str(log_name)


def cmdline_has_debug(cmdline):
    """Three-valued, and the NULL is the point.

    None means "this boot's /proc/cmdline was not readable", which is not
    the same claim as "verified clean" -- so the trend query filters with
    `IS NOT TRUE` and keeps unknowns rather than emptying the chart for
    every boot recorded before the cmdline was read.

    Delegates to the skill's `cmdline_has_debug_params`, which is built from
    the same patterns `ensure_debug_cmdline_params` applies. Re-deriving
    "what counts as a debug cmdline" here would let the exclusion rule drift
    away from what the script actually sets, and the symptom would be a
    debug-polluted boot quietly included in a default-phase trend.
    """
    if not cmdline or not str(cmdline).strip():
        return None
    return bb.get("cmdline_has_debug_params")(str(cmdline))


# ---------------------------------------------------------------------------
# the status document
# ---------------------------------------------------------------------------

def derive_status(doc) -> str:
    """The run_status a finished job earns from its own report.

    Exit code alone is not enough, and this is the distinction the whole
    system exists to make: `bootbench.py` exits 0 when it recorded *at least
    one* boot, because recording five of six beats recording none. A run
    that quietly drops a boot every night is a device degrading, and it must
    not render as a plain success.
    """
    exit_code = doc.get("exit_code")
    recorded = doc.get("boots_recorded") or 0
    expected = doc.get("boots_expected") or 0

    if exit_code != 0:
        return "failed"
    if recorded == 0:
        # Exit 0 with nothing recorded should be impossible; treat the
        # report rather than the exit code as authoritative.
        return "failed"
    if expected and recorded < expected:
        return "partial"
    return "success"


def run_fields_from_status(doc) -> dict:
    """The `run` columns a status document determines.

    Deliberately excludes every queue-side column -- status, queued_at,
    trigger_source, triggered_by, device_id. Those were decided when the run
    was enqueued, and an ingest that reset them would erase who asked for the
    run and why.
    """
    outputs = doc.get("outputs") or {}
    stages_run = _stages(doc.get("stages_run"))
    return {
        "target": doc.get("target"),
        "host": doc.get("host"),
        "build_number": doc.get("build_number"),
        "build_folder": doc.get("build_folder"),
        "build_path": doc.get("build_path"),
        "share_root": doc.get("share_root"),
        "exit_code": doc.get("exit_code"),
        "failure_stage": doc.get("failure_stage"),
        "error_class": doc.get("error_class"),
        "error_message": doc.get("error_message"),
        "stages_requested": _stages(doc.get("stages_requested")),
        "stages_run": stages_run,
        # Load-bearing for trending, not bookkeeping: the collect script
        # masks rmtfs.service persistently, so a run that did not reflash
        # started from a different system than the one before it.
        #
        # NULL when no stages were reported, which is the backfill case --
        # the skill's per-target JSON does not record which stages ran. The
        # column is nullable for exactly this, and `False` would be a
        # different and wrong claim: `reflashed IS NOT FALSE` would then
        # drop every backfilled run from the trend on the strength of a
        # guess.
        "reflashed": ("flash" in stages_run) if stages_run else None,
        "boots_expected": doc.get("boots_expected"),
        "boots_recorded": doc.get("boots_recorded") or 0,
        "started_at": _utc(doc.get("started_utc")),
        "finished_at": _utc(doc.get("ended_utc")),
        "status_json": doc,
        "schema_version": doc.get("schema_version"),
        "agent_data_json": outputs.get("data_json"),
        "agent_report_html": outputs.get("report_html"),
        "agent_boot_logs_dir": outputs.get("boot_logs_dir"),
    }


def boot_rows_from_status(doc) -> list:
    """One dict of `boot` columns per boot the job attempted.

    Every boot, including the ones that failed to parse: a boot row with
    `recorded = false` and a `parse_error` is how "this device dropped a
    boot on the 3rd of October" stays answerable. `boot_trend` filters them
    out, so they cost nothing to a chart and everything to a diagnosis.
    """
    entries = list(doc.get("boots") or [])
    parsed = [b for b in entries if not b.get("parse_error")]
    # Mirrors the skill's own rule via its own function, so "which boots did
    # the report show" has one definition rather than two that can drift.
    displayed = {id(b) for b in bb.pick_displayed(parsed)} if parsed else set()

    rows = []
    for seq, entry in enumerate(entries, start=1):
        seconds = entry.get("seconds") or {}
        cmdline = entry.get("kernel_cmdline")
        flagged = entry.get("cmdline_has_debug")
        log_name = entry.get("log_name") or f"boot-{seq}"
        row = {
            "log_name": log_name,
            # Fall back to the log name for both. `build_run` sets neither
            # -- pull_and_record attaches them afterwards -- so a document
            # built from the skill's own report JSON has only the name, and
            # a NULL boot_index is invisible to trend(), which pins it to 1.
            "phase": entry.get("phase") or phase_from_log_name(log_name),
            "boot_index": (entry.get("boot_index")
                           if entry.get("boot_index") is not None
                           else boot_index_from_log_name(log_name)),
            "seq": seq,
            "displayed": id(entry) in displayed,
            "recorded": not entry.get("parse_error"),
            "parse_error": entry.get("parse_error"),
            "bootbench_ts": entry.get("timestamp"),
            "hitters": entry.get("hitters") or None,
            "overall_overheads": entry.get("overall_overheads") or None,
            "critical_chain": entry.get("critical_chain") or None,
            "metric_notes": entry.get("metric_notes") or None,
            "kernel_cmdline": cmdline,
            # Trust the skill's own verdict when it recorded one; fall back
            # to reading the string, which is what makes a document written
            # before the field existed still usable.
            "cmdline_has_debug": (flagged if isinstance(flagged, bool)
                                  else cmdline_has_debug(cmdline)),
        }
        for key in schema.SECONDS_KEYS:
            row[schema.seconds_column(key)] = _sec(seconds.get(key))
        rows.append(row)
    return rows


def ingest_status(conn, run_id, doc, *, artifact_dir=None,
                  readopted=False) -> dict:
    """Write a status document's run fields and every boot it reports.

    One transaction, left uncommitted: the caller owns the commit, because
    the worker finalizes the run status in the same transaction and a run
    whose boots landed but whose status did not would be indistinguishable
    from a crashed ingest.
    """
    if not isinstance(doc, dict):
        raise IngestError("status document is not an object")

    version = doc.get("schema_version")
    if version is None:
        log.warning("run %s: status document has no schema_version", run_id)
    elif version > SUPPORTED_SCHEMA_VERSION:
        # Ingested anyway. The fields read here are additive, and refusing
        # would discard a real night's data over a field nobody reads.
        log.warning("run %s: status schema_version %s is newer than the "
                    "supported %s; ingesting the fields we know",
                    run_id, version, SUPPORTED_SCHEMA_VERSION)

    fields = run_fields_from_status(doc)
    if artifact_dir is not None:
        fields["artifact_dir"] = str(artifact_dir)
    if readopted:
        fields["readopted"] = True
    queries.set_run_fields(conn, run_id, **fields)

    rows = boot_rows_from_status(doc)
    for row in rows:
        queries.upsert_boot(conn, run_id, {"run_id": run_id, **row})

    recorded = sum(1 for r in rows if r["recorded"])
    status = derive_status(doc)
    log.info("run %s: ingested %d boot(s), %d recorded -> %s",
             run_id, len(rows), recorded, status)
    return {
        "run_id": run_id,
        "status": status,
        "boots": len(rows),
        "recorded": recorded,
        "parse_errors": [r["log_name"] for r in rows if r["parse_error"]],
    }


# ---------------------------------------------------------------------------
# backfill from the skill's own report
# ---------------------------------------------------------------------------

def _run_dict_to_status(run: dict, *, target, device_id) -> dict:
    """Shape one entry of the skill's per-target JSON like a status document.

    Converting rather than writing a second ingest path: the run columns and
    the boot columns are then populated by exactly the code that handles a
    live job, so a backfilled row and a real one cannot differ in shape. The
    conversion is lossy in one direction only -- the per-target JSON holds
    just the 2 displayed boots of each run, and no amount of care recovers
    the other 2N-2.
    """
    boots = []
    for boot in run.get("boots") or []:
        entry = {
            "phase": boot.get("phase"),
            "boot_index": boot.get("boot_index"),
            "log_name": boot.get("log_name"),
            "timestamp": boot.get("timestamp") or run.get("timestamp"),
            "kernel_cmdline": boot.get("kernel_cmdline"),
            "cmdline_has_debug": boot.get("cmdline_has_debug"),
            "critical_chain": boot.get("critical_chain"),
            "overall_overheads": boot.get("overall_overheads"),
            # `run_metrics_seconds` prefers the numeric `seconds` key and
            # falls back to parse_seconds over the formatted strings, which
            # is the whole reason history recorded before that key existed is
            # still ingestible.
            "seconds": bb.run_metrics_seconds(boot),
        }
        metrics = boot.get("metrics") or {}
        hitters = {k: m["hitters"] for k, m in metrics.items()
                   if isinstance(m, dict) and m.get("hitters")}
        notes = {k: m["note"] for k, m in metrics.items()
                 if isinstance(m, dict) and m.get("note")}
        entry["hitters"] = hitters or None
        # Only available on this path: the status document reports on the
        # job and deliberately does not carry the report's prose.
        entry["metric_notes"] = notes or None
        boots.append(entry)

    build_path = run.get("build_path")
    return {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "host": run.get("host"),
        "target": run.get("target") or target,
        "device_id": device_id,
        "build_path": build_path,
        "build_folder": (bb.build_folder_name(build_path)
                         if build_path else None),
        "build_number": (bb.parse_build_number(build_path)
                         if build_path else None),
        "share_root": None,
        "stages_requested": ["flash", "capture", "report"],
        # Unknowable from this file. `reflashed` is therefore left NULL
        # rather than guessed, and the trend's `IS NOT FALSE` filter keeps
        # the row -- an honest "not recorded" instead of a confident wrong
        # answer either way.
        "stages_run": [],
        "exit_code": 0,
        "boots_expected": len(boots),
        "boots_recorded": len(boots),
        "started_utc": None,
        "ended_utc": None,
        "outputs": {},
        "boots": boots,
        "backfilled_from": run.get("timestamp"),
    }


def ingest_from_target_json(conn, device_id, path, *, target=None,
                            limit=None) -> list:
    """Backfill history from a `bootchart-data-<slug>.json`.

    This is what makes Phase 5 worth having before any orchestration exists:
    point it at the file your manual runs have been appending to and the
    existing history becomes queryable in SQL. Two honest limitations, both
    inherent to the source rather than to this code:

      * only the 2 displayed boots per run are in the file at all
      * `started_at` is the skill's naive local minute stamp, so runs are
        ordered by build number and ingest order, not by a real timestamp
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    runs = data.get("runs") if isinstance(data, dict) else data
    if not isinstance(runs, list):
        raise IngestError(f"{path} has no 'runs' list")
    if limit:
        runs = runs[-limit:]

    results = []
    for run in runs:
        doc = _run_dict_to_status(run, target=target, device_id=device_id)
        run_id = queries.insert_backfilled_run(
            conn, device_id,
            target=doc["target"],
            build_number=doc["build_number"],
            build_folder=doc["build_folder"],
            build_path=doc["build_path"],
            bootbench_ts=run.get("timestamp"),
        )
        if run_id is None:
            # Already backfilled. Re-running the command after adding runs
            # must not duplicate the ones already there, and the skill's own
            # timestamp is the only identity this file offers.
            results.append({"run_id": None, "status": "skipped",
                            "bootbench_ts": run.get("timestamp")})
            continue
        summary = ingest_status(conn, run_id, doc)
        # set_run_fields, not finish_run: finish_run stamps finished_at with
        # now(), and a run that happened in March did not finish today. The
        # skill's own minute stamp is kept in status_json instead, which is
        # honest about being a display value rather than an ordering key.
        queries.set_run_fields(conn, run_id, status=summary["status"])
        results.append({**summary, "bootbench_ts": run.get("timestamp")})
    return results


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------

def reparse_boot_logs(conn, run_id, logs_dir, *, phases=("default", "debug"),
                      num_boots=3) -> dict:
    """Re-read a run's pulled log files and rewrite its boots.

    The repair path. Reaches for it when a boot failed to parse on the bench
    host -- a truncated dmesg, a systemd-analyze that returned nothing -- but
    the files themselves came back. Runs the skill's own `build_run` over
    them, which is why this is a repair and not a second parser.

    Rewrites rather than merges: boots are deleted first, so a log directory
    that has since been renamed cannot leave an orphan row the UPSERT never
    reaches.
    """
    logs_dir = Path(logs_dir)
    if not logs_dir.is_dir():
        raise IngestError(f"not a directory: {logs_dir}")

    build_run = bb.get("build_run")
    read_pulled_logs = bb.get("read_pulled_logs")
    has_debug = bb.get("cmdline_has_debug_params")

    entries, failures = [], []
    for phase in phases:
        for index in range(1, num_boots + 1):
            log_name = f"{phase}-{index}"
            if not (logs_dir / f"Logs-{log_name}").is_dir():
                continue
            try:
                # Both take (local_dir, log_name) and build_run takes the
                # six texts positionally -- it is the skill's internal
                # calling convention, mirrored rather than wrapped so a
                # signature change there fails here loudly.
                texts = read_pulled_logs(logs_dir, log_name)
                run = build_run(
                    None, None,
                    texts["sat_text"], texts["cc_text"],
                    texts["cc_sysinit_text"], texts["blame_text"],
                    texts["dmesg_text"], texts["journalctl_text"],
                )
            except Exception as e:     # noqa: BLE001 - any parse failure
                failures.append(log_name)
                entries.append({
                    "log_name": log_name, "phase": phase,
                    "boot_index": index,
                    "parse_error": f"{type(e).__name__}: {e}",
                })
                continue
            cmdline = texts.get("kernel_cmdline")
            entries.append({
                "log_name": log_name,
                "phase": phase,
                "boot_index": index,
                "timestamp": run.get("timestamp"),
                "kernel_cmdline": cmdline,
                "cmdline_has_debug": has_debug(cmdline),
                "critical_chain": run.get("critical_chain"),
                "overall_overheads": run.get("overall_overheads"),
                "seconds": bb.run_metrics_seconds(run),
                "hitters": {k: m["hitters"]
                            for k, m in (run.get("metrics") or {}).items()
                            if isinstance(m, dict) and m.get("hitters")}
                           or None,
                "metric_notes": {k: m["note"]
                                 for k, m in (run.get("metrics") or {}).items()
                                 if isinstance(m, dict) and m.get("note")}
                                or None,
            })

    if not entries:
        raise IngestError(
            f"{logs_dir} holds no Logs-<phase>-<n> directories")

    recorded = sum(1 for e in entries if not e.get("parse_error"))
    doc = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "boots": entries,
        "boots_expected": len(entries),
        "boots_recorded": recorded,
        "exit_code": 0,
        "outputs": {"boot_logs_dir": str(logs_dir)},
    }

    queries.delete_boots(conn, run_id)
    rows = boot_rows_from_status(doc)
    for row in rows:
        queries.upsert_boot(conn, run_id, {"run_id": run_id, **row})
    # Only the boot counts are corrected. The run's status, exit code and
    # failure stage are what the job actually did, and a later successful
    # reparse does not retroactively make a failed flash succeed.
    queries.set_run_fields(conn, run_id, boots_recorded=recorded,
                           boots_expected=len(entries))
    log.info("run %s: reparsed %d boot(s), %d recorded, %d still failing",
             run_id, len(rows), recorded, len(failures))
    return {"run_id": run_id, "boots": len(rows), "recorded": recorded,
            "parse_errors": failures}
