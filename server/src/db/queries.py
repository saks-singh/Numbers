"""Every SQL statement the coordinator runs.

Kept in one module so the schema has exactly one consumer to audit, and so
the column lists in schema.py are the only place a metric name is spelled.

One recurring detail, which is Risk 9 in the plan and the predictable first
bug of the web phase: every duration is NUMERIC, psycopg returns NUMERIC as
`Decimal`, and `json.dumps` refuses a `Decimal`. So anything that feeds the
dashboard casts `::float8` in SQL, at the edge, rather than converting in
Python afterwards -- a conversion in Python is easy to forget in one branch,
whereas a missing cast fails immediately and visibly in the one place the
query lives. NUMERIC is still right for storage: build-over-build deltas of
3-decimal seconds must not accumulate float artifacts.
"""

from __future__ import annotations

import logging

from .schema import (BOOT_COLUMNS, BOOT_JSON_COLUMNS, DEVICE_COLUMNS,
                     RUN_INGEST_COLUMNS, SECONDS_COLUMNS)

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
BENCHMARKED_STATUSES = ("success", "partial")
# Every value of the run_status enum, in the schema's order, for the
# inventory page's filter. Spelled here rather than queried so the filter
# still renders when the database is unreachable.
ALL_STATUSES = ("queued", "running", "success", "partial", "failed",
                "timeout", "unreachable", "cancelled")

# Columns typed as a Postgres enum rather than TEXT. Every one of them needs
# an explicit `::<type>` on its placeholder: psycopg sends a Python str as
# `text`, and Postgres has no `run_status = text` operator, so an uncast
# comparison fails with "operator does not exist" and an uncast assignment
# fails with "column is of type run_status but expression is of type text".
# Enums are still the right choice -- they reject a typo'd status at write
# time -- but the cast is not optional.
ENUM_COLUMNS = {
    "status": "run_status",
    "trigger_source": "trigger_source",
    "last_action": "decision_action",
    "action": "decision_action",
}


def _enum_cast(column: str) -> str:
    """`%s` or `%s::run_status`, depending on the column."""
    enum = ENUM_COLUMNS.get(column)
    return f"%s::{enum}" if enum else "%s"


def _dict_row():
    from psycopg.rows import dict_row

    return dict_row


def _jsonb(value):
    """Wrap a Python object for a JSONB column, passing NULL through.

    `Jsonb(None)` would store the JSON value `null`, which is not the same
    as a SQL NULL -- `IS NULL` would be false and `jsonb_array_length`
    would raise. The distinction matters for `overall_overheads`, where
    "no hitters computed" and "hitters computed, empty" are different.
    """
    if value is None:
        return None
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def _placeholders(columns) -> str:
    return ", ".join(["%s"] * len(columns))


def _float_selection(prefix="") -> str:
    """`col::float8 AS col` for every duration column.

    Built from SECONDS_COLUMNS rather than written out, so a metric added to
    the skill flows into the charts without editing a SELECT list -- and,
    more importantly, cannot be half-added.
    """
    return ", ".join(f"{prefix}{c}::float8 AS {c}" for c in SECONDS_COLUMNS)


# ---------------------------------------------------------------------------
# device projection
# ---------------------------------------------------------------------------

def upsert_device(conn, device) -> None:
    """Project one inventory Device into the table.

    last_seen_at is refreshed on every upsert; first_seen_at deliberately is
    not, so "this board has been benchmarked since March" survives a config
    reload.
    """
    values = [
        device.device_id, device.target, device.slug, device.agent_url,
        device.enabled, device.com_port, device.tac_port, device.adb_serial,
        device.num_boots, device.boot_timeout, device.schedule, device.stages,
        device.fetch_full_logs, device.max_retries, device.notes,
    ]
    assert len(values) == len(DEVICE_COLUMNS)
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in DEVICE_COLUMNS if c != "device_id")
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO device ({', '.join(DEVICE_COLUMNS)}) "
            f"VALUES ({_placeholders(DEVICE_COLUMNS)}) "
            f"ON CONFLICT (device_id) DO UPDATE SET {updates}, "
            f"last_seen_at = now()",
            values,
        )


def upsert_inventory(conn, inventory) -> int:
    for device in inventory:
        upsert_device(conn, device)
    return len(inventory)


def devices(conn, enabled_only=False) -> list:
    sql = "SELECT * FROM device"
    if enabled_only:
        sql += " WHERE enabled"
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(sql + " ORDER BY device_id")
        return cur.fetchall()


# ---------------------------------------------------------------------------
# the queue: run rows
# ---------------------------------------------------------------------------

def enqueue_run(conn, device, *, trigger_source="cron", triggered_by=None,
                build=None, parent_run_id=None, stages=None) -> int:
    """Insert a queued run and return its id. The row IS the job.

    Build fields are recorded at enqueue time when the scheduler already
    knows them (it asked the agent for the latest build to decide whether to
    run at all), so the dashboard can say which build a queued run is for
    before the agent has done anything.
    """
    build = build or {}
    stage_list = (stages or device.stages or "all").split()
    if stage_list == ["all"]:
        stage_list = ["flash", "capture", "report"]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO run (device_id, target, agent_url, build_number,
                             build_folder, build_path, share_root,
                             status, trigger_source, triggered_by,
                             parent_run_id, stages_requested, boots_expected)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    'queued', %s::trigger_source, %s, %s, %s, %s)
            RETURNING run_id
            """,
            (device.device_id, device.target, device.agent_url,
             build.get("build_number"), build.get("build_folder"),
             build.get("build_path"), build.get("share_root"),
             trigger_source, triggered_by, parent_run_id,
             stage_list, device.boots_expected),
        )
        (run_id,) = cur.fetchone()

    # The worker is a separate process; this wakes it without waiting for its
    # poll interval. Same transaction as the INSERT, so a rolled-back enqueue
    # cannot notify about a run that does not exist.
    with conn.cursor() as cur:
        cur.execute("SELECT pg_notify('bootbench_jobs', %s)", (str(run_id),))
    return run_id


def active_run(conn, device_id):
    """The queued-or-running run for a device, if any. The overlap check."""
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT run_id, status, queued_at, started_at, trigger_source "
            "FROM run WHERE device_id = %s AND status = ANY(%s::run_status[]) "
            "ORDER BY queued_at LIMIT 1",
            (device_id, list(ACTIVE_STATUSES)),
        )
        return cur.fetchone()


def claim_next_run(conn, exclude_devices=None):
    """Claim the oldest queued run, skipping rows another worker holds.

    FOR UPDATE SKIP LOCKED is what makes two workers safe without a separate
    lock table: each takes a different row instead of both taking the same
    one or one blocking on the other.

    `exclude_devices` skips devices this worker already knows it cannot lock.
    Without it, a queued run for a busy device would be claimed, found
    unlockable, rolled back, and claimed again on the next poll forever --
    starving a second device whose run sits behind it in queued_at order.
    """
    params = []
    sql = "SELECT run_id FROM run WHERE status = 'queued'"
    if exclude_devices:
        sql += " AND device_id <> ALL(%s)"
        params.append(list(exclude_devices))
    sql += " ORDER BY queued_at FOR UPDATE SKIP LOCKED LIMIT 1"

    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE run SET status = 'running', started_at = now() "
            "WHERE run_id = %s RETURNING *",
            (row["run_id"],),
        )
        return cur.fetchone()


def adoptable_runs(conn):
    """Runs a worker must adopt rather than claim.

    A `running` row with an agent_job_id is a job that was dispatched and
    then lost its watcher: the worker restarted, or was killed, while the
    bench host carried on regardless. `claim_next_run` will never return it
    -- it only looks at `queued` -- so without this the row sits in
    `running` forever, the device it names stays blocked by the overlap
    check, and the finished job and its artifacts sit unread on the agent.

    The agent does the same thing from its side on startup (adopt a live
    pid, finalize a dead one from its status document). This is the
    coordinator's half of that.
    """
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT * FROM run WHERE status = 'running' "
            "AND agent_job_id IS NOT NULL ORDER BY started_at"
        )
        return cur.fetchall()


def try_lock_device(conn, device_id) -> bool:
    """Session-level advisory lock, held by the worker's own connection.

    Auto-released if the worker process dies, so a crash cannot wedge a
    device forever -- which a status column or a lock table would.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            (f"bootbench:device:{device_id}",),
        )
        (got,) = cur.fetchone()
        return bool(got)


def unlock_device(conn, device_id) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_unlock(hashtext(%s))",
            (f"bootbench:device:{device_id}",),
        )


def set_run_fields(conn, run_id, **fields) -> None:
    """Update named columns on a run. Used for the small state transitions
    (agent_job_id, last_output_at, cancel_requested) that are not worth a
    function each."""
    if not fields:
        return
    allowed = set(RUN_INGEST_COLUMNS) | {
        "status", "agent_job_id", "artifact_dir", "last_output_at",
        "cancel_requested", "acknowledged_at", "readopted", "finished_at",
        "started_at",
    }
    unknown = set(fields) - allowed
    if unknown:
        # A typo'd column name would otherwise be a silent no-op or a
        # confusing psycopg error far from its cause.
        raise ValueError(f"not an updatable run column: {sorted(unknown)}")

    assignments = ", ".join(f"{k} = {_enum_cast(k)}" for k in fields)
    values = [
        _jsonb(v) if k in ("status_json",) else v for k, v in fields.items()
    ]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE run SET {assignments} WHERE run_id = %s",
                    [*values, run_id])


def upsert_boot(conn, run_id, values) -> int:
    """Write one boot, replacing any earlier row for the same log_name.

    The UPSERT on (run_id, log_name) is what makes ingestion idempotent, and
    that is load-bearing rather than defensive: a run re-adopted after an
    agent restart is ingested once from the live job and again from its
    status file, and `reparse_boot_logs` deliberately ingests a third time to
    repair a boot the bench host failed to parse. All three must converge on
    the same row instead of producing three.

    `values` is keyed by column name; anything absent is written as NULL, so
    a repair that knows more than the original ingest overwrites it and one
    that knows less does not leave a stale number behind.
    """
    unknown = set(values) - set(BOOT_COLUMNS)
    if unknown:
        raise ValueError(f"not a boot column: {sorted(unknown)}")

    columns = list(BOOT_COLUMNS)
    row = [
        _jsonb(values.get(c)) if c in BOOT_JSON_COLUMNS else values.get(c)
        for c in columns
    ]
    updates = ", ".join(f"{c} = EXCLUDED.{c}"
                        for c in columns if c not in ("run_id", "log_name"))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO boot ({', '.join(columns)}) "
            f"VALUES ({_placeholders(columns)}) "
            f"ON CONFLICT (run_id, log_name) DO UPDATE SET {updates} "
            f"RETURNING boot_id",
            row,
        )
        (boot_id,) = cur.fetchone()
    return boot_id


def delete_boots(conn, run_id) -> int:
    """Drop every boot of a run. Used by a reparse that rebuilds the whole
    set, where a boot that has since been renamed would otherwise survive as
    an orphan the UPSERT never touches."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM boot WHERE run_id = %s", (run_id,))
        return cur.rowcount


def insert_backfilled_run(conn, device_id, *, target=None, build_number=None,
                          build_folder=None, build_path=None,
                          bootbench_ts=None):
    """Create a run row for one historical entry, or None if it is already in.

    Backfill is run by hand, usually more than once -- you point it at the
    report file, add a few more nightlies by hand, and point it again. So it
    needs an identity to recognise an entry it has already taken, and the
    skill's per-target JSON offers exactly one: `timestamp`, its naive local
    minute stamp. Not a good key, but the only one, and the alternative is
    duplicating the whole history on every re-run.

    Stored in status_json rather than as a column because it is a property
    of the backfill, not of a run: a real run has started_at and does not
    need it. The dedupe query is a sequential scan over a small set of
    backfilled rows, run once per entry per manual invocation.
    """
    with conn.cursor() as cur:
        if bootbench_ts:
            cur.execute(
                "SELECT run_id FROM run WHERE device_id = %s "
                "AND trigger_source = 'backfill' "
                "AND status_json->>'backfilled_from' = %s LIMIT 1",
                (device_id, str(bootbench_ts)),
            )
            existing = cur.fetchone()
            if existing:
                return None

        cur.execute(
            "INSERT INTO run (device_id, target, build_number, build_folder, "
            "                 build_path, status, trigger_source, "
            "                 stages_requested) "
            "VALUES (%s, %s, %s, %s, %s, 'queued'::run_status, "
            "        'backfill'::trigger_source, %s) "
            "RETURNING run_id",
            (device_id, target, build_number, build_folder, build_path,
             ["flash", "capture", "report"]),
        )
        (run_id,) = cur.fetchone()
    return run_id


def finish_run(conn, run_id, status, **fields) -> None:
    """Set the terminal status and stamp finished_at in one statement.
    finished_at defaults to now() but is overridable, because a run
    finalized from a re-adopted status document finished when the status
    document says it did -- not when the worker got around to noticing.
    """
    finished_at = fields.pop("finished_at", None)
    if finished_at is None:
        set_run_fields(conn, run_id, status=status, **fields)
        with conn.cursor() as cur:
            cur.execute("UPDATE run SET finished_at = now() "
                        "WHERE run_id = %s AND finished_at IS NULL",
                        (run_id,))
    else:
        set_run_fields(conn, run_id, status=status,
                       finished_at=finished_at, **fields)


# ---------------------------------------------------------------------------
# the scheduler's questions
# ---------------------------------------------------------------------------

def is_build_benchmarked(conn, device_id, build_number) -> bool:
    """Has this device already measured this build?

    Deliberately a query, not a unique constraint: `--force` re-runs the
    same build on purpose, to measure run-to-run variance or to retry after
    a transient failure. Partial counts as benchmarked -- the build was
    measured, just not completely.
    """
    if build_number is None:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM run WHERE device_id = %s AND build_number = %s "
            "AND status = ANY(%s::run_status[]) LIMIT 1",
            (device_id, build_number, list(BENCHMARKED_STATUSES)),
        )
        return cur.fetchone() is not None


def last_run(conn, device_id):
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT run_id, status, build_number, acknowledged_at, "
            "       exit_code, failure_stage, finished_at, trigger_source "
            "FROM run WHERE device_id = %s "
            "ORDER BY queued_at DESC, run_id DESC LIMIT 1",
            (device_id,),
        )
        return cur.fetchone()


def last_success(conn, device_id):
    """The newest fully-successful run. `partial` does not count here even
    though it counts as benchmarked: the staleness alert exists to catch a
    device that is quietly degrading, and a run of partials is exactly that."""
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT run_id, build_number, finished_at FROM run "
            "WHERE device_id = %s AND status = 'success' "
            "ORDER BY finished_at DESC NULLS LAST, run_id DESC LIMIT 1",
            (device_id,),
        )
        return cur.fetchone()


def retry_depth(conn, run_id) -> int:
    """How many retries led to this run: 0 for an original, 1 for its retry.

    Walked through parent_run_id rather than counted by trigger_source, so
    `max_retries` bounds one chain of attempts at a single build instead of
    the total number of retries a device has ever had.
    """
    depth = 0
    seen = set()
    with conn.cursor() as cur:
        current = run_id
        while current is not None and current not in seen and depth < 64:
            seen.add(current)
            cur.execute("SELECT parent_run_id FROM run WHERE run_id = %s",
                        (current,))
            row = cur.fetchone()
            if row is None or row[0] is None:
                break
            current = row[0]
            depth += 1
    return depth


def get_scheduler_state(conn, device_id):
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute("SELECT * FROM scheduler_state WHERE device_id = %s",
                    (device_id,))
        return cur.fetchone()


def touch_scheduler_state(conn, device_id, *, tick_at=None, action=None,
                          reason=None, errored=False) -> None:
    """Record that this device was evaluated.

    consecutive_errors is reset by any non-error decision, so the "4
    consecutive failed ticks" alert measures a persistent condition rather
    than a count that only ever grows.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scheduler_state
                (device_id, last_tick_at, last_decision_at, last_action,
                 last_reason, consecutive_errors)
            VALUES (%s, COALESCE(%s, now()), now(), %s::decision_action,
                    %s, %s)
            ON CONFLICT (device_id) DO UPDATE SET
                last_tick_at = COALESCE(EXCLUDED.last_tick_at, now()),
                last_decision_at = now(),
                last_action = EXCLUDED.last_action,
                last_reason = EXCLUDED.last_reason,
                consecutive_errors = CASE
                    WHEN %s THEN scheduler_state.consecutive_errors + 1
                    ELSE 0 END
            """,
            (device_id, tick_at, action, reason, 1 if errored else 0, errored),
        )


def record_decision(conn, device_id, action, reason, *, build_number=None,
                    run_id=None, detail=None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scheduler_decision "
            "(device_id, action, reason, build_number, run_id, detail) "
            "VALUES (%s, %s::decision_action, %s, %s, %s, %s) "
            "RETURNING decision_id",
            (device_id, action, reason, build_number, run_id, _jsonb(detail)),
        )
        (decision_id,) = cur.fetchone()
        return decision_id


def recent_decisions(conn, device_id=None, limit=100) -> list:
    sql = ("SELECT d.*, dev.target FROM scheduler_decision d "
           "LEFT JOIN device dev USING (device_id)")
    params = []
    if device_id:
        sql += " WHERE d.device_id = %s"
        params.append(device_id)
    sql += " ORDER BY d.decided_at DESC, d.decision_id DESC LIMIT %s"
    params.append(limit)
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# the dashboard's questions
# ---------------------------------------------------------------------------

def latest_per_device(conn) -> list:
    """One row per device: its newest run, plus the newest comparable boot
    time. Drives the index page."""
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            f"""
            SELECT d.device_id, d.target, d.agent_url, d.enabled, d.notes,
                   r.run_id, r.status, r.build_number, r.build_folder,
                   r.queued_at, r.started_at, r.finished_at,
                   r.exit_code, r.failure_stage, r.boots_recorded,
                   r.boots_expected, r.reflashed,
                   b.total_multiuser_s::float8 AS total_multiuser_s
            FROM device d
            LEFT JOIN run_latest r USING (device_id)
            LEFT JOIN boot b ON b.run_id = r.run_id
                 AND b.phase = 'default' AND b.boot_index = 1 AND b.recorded
            ORDER BY d.device_id
            """
        )
        return cur.fetchall()


def run_detail(conn, run_id):
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute("SELECT * FROM run WHERE run_id = %s", (run_id,))
        return cur.fetchone()


def boots_for_run(conn, run_id) -> list:
    """Every boot of a run -- all 2N, failures included, in pull order.

    A failed boot is not filtered out here: the run page exists partly to
    show which boot failed and why, and a silently shorter table would hide
    exactly that.
    """
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            f"""
            SELECT boot_id, run_id, log_name, phase, boot_index, seq,
                   displayed, recorded, parse_error, bootbench_ts,
                   kernel_cmdline, cmdline_has_debug,
                   hitters, overall_overheads, critical_chain, metric_notes,
                   {_float_selection()}
            FROM boot WHERE run_id = %s
            ORDER BY COALESCE(seq, 0), boot_id
            """,
            (run_id,),
        )
        return cur.fetchall()


def recent_runs(conn, device_id, limit=30) -> list:
    """Run history for a device page, with the comparable boot's numbers."""
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            f"""
            SELECT r.run_id, r.status, r.build_number, r.build_folder,
                   r.queued_at, r.started_at, r.finished_at, r.exit_code,
                   r.failure_stage, r.trigger_source, r.reflashed,
                   r.boots_recorded, r.boots_expected,
                   {_float_selection('b.')}
            FROM run r
            LEFT JOIN boot b ON b.run_id = r.run_id
                 AND b.phase = 'default' AND b.boot_index = 1 AND b.recorded
            WHERE r.device_id = %s
            ORDER BY r.queued_at DESC, r.run_id DESC
            LIMIT %s
            """,
            (device_id, limit),
        )
        return cur.fetchall()


def all_runs(conn, *, device_id=None, status=None, limit=200) -> list:
    """Every run across every device, newest first.

    Backs the runs/logs inventory. Ordered by `queued_at` rather than
    `started_at` so a run that is still queued -- which has no start time
    yet -- sorts to the top, where whoever just pressed Run now will look
    for it rather than wondering whether the click registered.

    Selects the provenance columns the run page shows (`build_path`,
    `share_root`, `host`, `artifact_dir`) so the inventory can answer "which
    build, from which share, mirrored to where" without a second query per
    row.
    """
    sql = """
        SELECT r.run_id, r.device_id, r.target, r.agent_url, r.host,
               r.status, r.exit_code, r.failure_stage,
               r.build_number, r.build_folder, r.build_path, r.share_root,
               r.queued_at, r.started_at, r.finished_at,
               r.trigger_source, r.triggered_by, r.reflashed,
               r.boots_recorded, r.boots_expected, r.artifact_dir,
               b.total_multiuser_s::float8 AS total_multiuser_s
        FROM run r
        LEFT JOIN boot b ON b.run_id = r.run_id
             AND b.phase = 'default' AND b.boot_index = 1 AND b.recorded
    """
    clauses, params = [], []
    if device_id:
        clauses.append("r.device_id = %s")
        params.append(device_id)
    if status:
        clauses.append(f"r.status = {_enum_cast('status')}")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY r.queued_at DESC, r.run_id DESC LIMIT %s"
    params.append(limit)

    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def run_counts(conn) -> dict:
    """`{status: n}` over the whole table, for the inventory's header."""
    with conn.cursor() as cur:
        cur.execute("SELECT status::text, count(*) FROM run GROUP BY 1")
        return {status: count for status, count in cur.fetchall()}


def trend(conn, device_id, *, phase="default", boot_index=1, limit=60,
          exclude_debug_cmdline=True, reflashed_only=False) -> list:
    """The chart series, oldest first, as plain floats.

    Pinned to one phase and one boot_index by default because the collect
    script masks rmtfs.service persistently: boot 1 of a run and boot 3 of
    the same run are different systems. Mixing them produces a sawtooth that
    looks like a real regression.

    `exclude_debug_cmdline` drops a boot whose own /proc/cmdline carried the
    debug params despite being labelled 'default'. `IS NOT TRUE` rather than
    `= false` deliberately keeps NULL -- unverified -- boots in the series:
    excluding every boot recorded before cmdline capture existed would
    silently empty the chart.
    """
    sql = f"""
        SELECT run_id, build_number, build_folder, started_at, status,
               log_name, phase, boot_index, reflashed, cmdline_has_debug,
               {_float_selection()},
               overall_overheads
        FROM boot_trend
        WHERE device_id = %s AND phase = %s AND boot_index = %s
    """
    params = [device_id, phase, boot_index]
    if exclude_debug_cmdline and phase == "default":
        sql += " AND cmdline_has_debug IS NOT TRUE"
    if reflashed_only:
        sql += " AND reflashed IS NOT FALSE"
    # Newest N, then flipped to chronological order for plotting. Doing it
    # the other way round would chart the oldest N and never move.
    sql += " ORDER BY build_number DESC NULLS LAST, started_at DESC LIMIT %s"
    params.append(limit)

    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return list(reversed(rows))


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

def runs_to_prune(conn, *, keep_days=None, keep_per_device=None) -> list:
    """Runs older than the retention policy, with the artifact dir to unlink.

    Returned rather than deleted so `gc --dry-run` is the same code path as
    `gc`, and so the caller can remove the mirrored files before the row
    naming them is gone. An active run is never a candidate however old it
    looks -- a queued row with an ancient queued_at is a backlog, not
    garbage.

    Both limits are applied, and a run has to fail only one of them to go:
    `keep_days` bounds the history in time, `keep_per_device` bounds it in
    rows so one very chatty device cannot crowd out the rest.
    """
    clauses, params = [], []
    if keep_days:
        clauses.append(
            "COALESCE(finished_at, started_at, queued_at) "
            "< now() - make_interval(days => %s)")
        params.append(int(keep_days))
    if keep_per_device:
        # row_number() over the device's own history, newest first.
        clauses.append("rn > %s")
        params.append(int(keep_per_device))
    if not clauses:
        return []

    sql = f"""
        SELECT run_id, device_id, build_number, status, artifact_dir,
               COALESCE(finished_at, started_at, queued_at) AS aged_at
        FROM (
            SELECT r.*, row_number() OVER (
                       PARTITION BY device_id
                       ORDER BY queued_at DESC, run_id DESC) AS rn
            FROM run r
            WHERE status <> ALL(%s::run_status[])
        ) ranked
        WHERE {' OR '.join(clauses)}
        ORDER BY run_id
    """
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(sql, [list(ACTIVE_STATUSES), *params])
        return cur.fetchall()


def delete_runs(conn, run_ids) -> int:
    """Delete runs by id. `boot` cascades; `scheduler_decision.run_id` is
    ON DELETE SET NULL, so the audit trail survives the run it refers to --
    "why didn't it run" must stay answerable after retention."""
    run_ids = [int(r) for r in run_ids]
    if not run_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM run WHERE run_id = ANY(%s)", (run_ids,))
        return cur.rowcount


def delete_decisions_before(conn, cutoff_days) -> int:
    """Trim the decision audit log. It gains a row per device per tick --
    ~96/day at a 15-minute tick -- so it outgrows the runs it explains."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM scheduler_decision "
            "WHERE decided_at < now() - make_interval(days => %s)",
            (int(cutoff_days),),
        )
        return cur.rowcount


def json_safe(rows):
    """Last-resort guard for anything heading into json.dumps.

    Every query above casts ::float8, so this should find nothing. It exists
    because a Decimal reaching json.dumps raises deep inside Flask's
    serializer, where the traceback does not name the column -- and a new
    query is the likeliest place for a missing cast.
    """
    import datetime
    import decimal

    def convert(value):
        if isinstance(value, decimal.Decimal):
            log.warning("Decimal reached json_safe -- a query is missing a "
                        "::float8 cast")
            return float(value)
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        return value

    return convert(rows)
