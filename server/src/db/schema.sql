-- bootbench coordinator schema.
--
-- Applied by `main.py initdb`, which runs every file in migrations/ in
-- name order and records it in schema_migration. This file is the current
-- consolidated shape, kept for reading and for `psql -f` on a fresh box;
-- migrations/ is what actually executes. Keep them in step.
--
-- Two conventions worth knowing before reading:
--
--   * Every duration column is NUMERIC(10,3) and named <metric>_s. Boot
--     times arrive as 3-decimal seconds (the skill formats them with
--     "{:.3f}"), and NUMERIC stores those exactly -- so AVG, percentiles,
--     and build-over-build deltas can't accumulate float artifacts and a
--     "-0.000 s" delta is unrepresentable. The cost is that psycopg hands
--     back Decimal, which json.dumps refuses; queries feeding the charts
--     cast ::float8 at the edge.
--
--   * `run` doubles as the job queue. A queued row IS the job: the web app
--     inserts one, the worker claims it with FOR UPDATE SKIP LOCKED, the
--     dashboard lists it. There is no second queue to drift out of sync,
--     and run_id is also the agent's job_id -- one number spans the URL,
--     the agent's job directory, and the status filename.

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------
-- Enums rather than TEXT + CHECK so a typo is rejected at write time by the
-- type itself. Extending one needs `ALTER TYPE ... ADD VALUE` in a
-- migration; that is a deliberate speed bump on a vocabulary the whole
-- dashboard reads.

DO $$ BEGIN
    CREATE TYPE run_status AS ENUM (
        'queued',       -- inserted, not yet claimed by a worker
        'running',      -- claimed; the agent has (or is about to have) the job
        'success',      -- exit 0, every expected boot recorded
        'partial',      -- exit 0, but boots_recorded < boots_expected
        'failed',       -- non-zero exit
        'timeout',      -- exceeded the per-run budget; board may need recovery
        'unreachable',  -- agent never answered; blocks the device pending ack
        'cancelled'     -- stopped at a safe point on request
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE trigger_source AS ENUM ('cron', 'manual', 'retry', 'backfill');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE decision_action AS ENUM (
        'enqueued',  -- a run was created
        'skipped',   -- nothing to do, and that is correct (not due, no new build)
        'blocked',   -- needs a human before this device runs again
        'error'      -- the decision itself failed; retried next tick
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- ---------------------------------------------------------------------------
-- device
-- ---------------------------------------------------------------------------
-- A projection of devices.yaml, upserted on every start and tick. The YAML
-- stays the source of truth -- the dashboard is read-only by design -- and
-- this table exists so a run carries a real foreign key and a run belonging
-- to a since-deleted device still resolves a name instead of rendering as a
-- bare string.

CREATE TABLE IF NOT EXISTS device (
    device_id       TEXT PRIMARY KEY,
    target          TEXT NOT NULL,
    slug            TEXT NOT NULL,
    agent_url       TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    com_port        TEXT,
    tac_port        TEXT,
    adb_serial      TEXT,
    num_boots       INTEGER NOT NULL DEFAULT 3,
    boot_timeout    INTEGER NOT NULL DEFAULT 480,
    schedule        TEXT NOT NULL DEFAULT '0 1 * * *',
    stages          TEXT NOT NULL DEFAULT 'all',
    fetch_full_logs BOOLEAN NOT NULL DEFAULT FALSE,
    max_retries     INTEGER NOT NULL DEFAULT 1,
    notes           TEXT NOT NULL DEFAULT '',
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- run
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS run (
    run_id            BIGSERIAL PRIMARY KEY,

    device_id         TEXT NOT NULL REFERENCES device (device_id),
    -- Denormalized on purpose: a run is a historical record. If the device
    -- is later repointed at another agent or its target is corrected, the
    -- run must keep reporting what it actually ran against.
    target            TEXT,
    agent_url         TEXT,
    host              TEXT,           -- bench hostname, from the status doc

    -- build_path is the shared '...\performance' directory and is IDENTICAL
    -- for every target built from the same nightly, so the target is NOT
    -- derivable from it. target comes from the inventory and the status doc.
    build_number      INTEGER,        -- NULL: only master_<n> nightlies parse
    build_folder      TEXT,
    build_path        TEXT,
    share_root        TEXT,

    status            run_status NOT NULL DEFAULT 'queued',
    exit_code         INTEGER,
    failure_stage     TEXT,           -- build_discovery|tac|edl|flash|...
    error_class       TEXT,
    error_message     TEXT,

    trigger_source    trigger_source NOT NULL DEFAULT 'cron',
    triggered_by      TEXT,           -- who, for a manual trigger
    parent_run_id     BIGINT REFERENCES run (run_id) ON DELETE SET NULL,
    cancel_requested  BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at   TIMESTAMPTZ,    -- a human cleared an 'unreachable' block

    stages_requested  TEXT[] NOT NULL DEFAULT '{}',
    stages_run        TEXT[] NOT NULL DEFAULT '{}',
    -- Whether this run actually reflashed. Load-bearing for trending: the
    -- collect script masks rmtfs.service persistently, so a capture-only run
    -- starts from a different system than the run before it.
    reflashed         BOOLEAN,
    boots_expected    INTEGER,
    boots_recorded    INTEGER NOT NULL DEFAULT 0,

    queued_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    -- Last time the agent produced any output. Distinguishes "wedged" from
    -- "slow" without shortening the timeout.
    last_output_at    TIMESTAMPTZ,

    -- The whole --json-status document, verbatim. Anything this schema does
    -- not model is still recoverable without re-reading the bench host.
    status_json       JSONB,
    schema_version    INTEGER,        -- the status doc's own version

    agent_job_id      TEXT,           -- normally run_id as text
    artifact_dir      TEXT,           -- coordinator-side mirror
    agent_data_json   TEXT,           -- paths ON THE BENCH HOST, not here
    agent_report_html TEXT,
    agent_boot_logs_dir TEXT,
    -- The agent was restarted mid-job and this run was finalized from the
    -- status file rather than observed to completion.
    readopted         BOOLEAN NOT NULL DEFAULT FALSE
);

-- The already-benchmarked probe the scheduler runs every tick. Partial
-- counts as benchmarked: the build was measured, just not completely.
CREATE INDEX IF NOT EXISTS run_device_build_idx
    ON run (device_id, build_number DESC)
    WHERE status IN ('success', 'partial');

-- Trends and run history.
CREATE INDEX IF NOT EXISTS run_device_started_idx
    ON run (device_id, started_at DESC);

-- The claim/overlap check. Tiny by construction -- at most a handful of rows
-- are ever live -- so this stays cheap however long the history grows.
CREATE INDEX IF NOT EXISTS run_active_idx
    ON run (device_id, queued_at)
    WHERE status IN ('queued', 'running');


-- ---------------------------------------------------------------------------
-- boot
-- ---------------------------------------------------------------------------
-- One row per boot ACTUALLY ATTEMPTED -- all 2N of them, not just the 2 the
-- skill's own JSON report displays. That is the whole reason this database
-- exists: the report keeps 30 runs and 2 boots each, and can never answer
-- "show me every boot of every run".

CREATE TABLE IF NOT EXISTS boot (
    boot_id        BIGSERIAL PRIMARY KEY,
    run_id         BIGINT NOT NULL REFERENCES run (run_id) ON DELETE CASCADE,

    log_name       TEXT NOT NULL,     -- 'default-1', 'debug-3', ...
    -- phase and boot_index are first-class for a reason that bites anyone
    -- trending naively: the collect script disables AND MASKS rmtfs.service,
    -- persistently. Within one run, default-1 boots with it enabled and
    -- default-2/3 boot with it masked. Those are not the same system, and
    -- the step change looks exactly like a real regression. Compare like
    -- for like; boot_trend defaults to boot_index = 1.
    phase          TEXT,
    boot_index     INTEGER,
    seq            INTEGER,           -- order pulled, for a stable tiebreak
    displayed      BOOLEAN NOT NULL DEFAULT FALSE,  -- shown in the skill report
    recorded       BOOLEAN NOT NULL DEFAULT FALSE,  -- parsed successfully
    parse_error    TEXT,
    -- The skill's own naive, minute-granularity local timestamp. Stored ONLY
    -- to cross-reference the per-boot .txt backup filenames. Never order by
    -- it: run.started_at is TIMESTAMPTZ and is what orders runs.
    bootbench_ts   TEXT,

    -- The six metrics the report table shows.
    nhlos_s            NUMERIC(10,3),
    kernel_s           NUMERIC(10,3),
    initramfs_s        NUMERIC(10,3),
    sysinit_svc_s      NUMERIC(10,3),
    total_sysinit_s    NUMERIC(10,3),
    total_multiuser_s  NUMERIC(10,3),
    -- Sub-components that used to exist only inside prose note strings.
    grand_total_s      NUMERIC(10,3),
    firmware_s         NUMERIC(10,3),
    loader_s           NUMERIC(10,3),
    userspace_s        NUMERIC(10,3),
    sat_total_s        NUMERIC(10,3),
    init_exec_s        NUMERIC(10,3),
    epoch_advanced_s   NUMERIC(10,3),
    systemd_running_s  NUMERIC(10,3),
    sysinit_target_s   NUMERIC(10,3),
    cc_multiuser_s     NUMERIC(10,3),

    hitters            JSONB,  -- {metric_key: [hitter, ...]}
    overall_overheads  JSONB,  -- pooled top-3 across hitter sources
    critical_chain     JSONB,  -- ordered unit names
    metric_notes       JSONB,  -- the prose note per metric, for the UI

    -- This boot's own /proc/cmdline, which the collect script already writes
    -- and the skill's parsers never read.
    kernel_cmdline     TEXT,
    -- DELIBERATELY NULLABLE AND THREE-VALUED. true/false when the cmdline was
    -- readable; NULL means "not known", which is not the same claim as
    -- "verified clean". ensure_debug_cmdline_params never reverts the debug
    -- params unless asked, and systemd.log_level=debug measurably slows boot
    -- -- so a boot labelled 'default' that really ran debug-y must be
    -- excludable from a trend rather than silently skewing it.
    cmdline_has_debug  BOOLEAN,

    UNIQUE (run_id, log_name)
);

-- Re-ingest is idempotent on (run_id, log_name) above, which matters because
-- a re-adopted run and a reparse repair both ingest the same run twice.

CREATE INDEX IF NOT EXISTS boot_run_idx ON boot (run_id);

-- Supports "which runs mention this service at all", which is how you chase
-- a regression back to the build that introduced it.
CREATE INDEX IF NOT EXISTS boot_overheads_gin_idx
    ON boot USING gin (overall_overheads);


-- ---------------------------------------------------------------------------
-- scheduler
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scheduler_state (
    device_id          TEXT PRIMARY KEY REFERENCES device (device_id) ON DELETE CASCADE,
    -- croniter is asked "was there a fire time in (last_tick_at, now]?"
    -- rather than "is it 01:00 right now", so a window missed because the
    -- server was down is still caught later the same day.
    last_tick_at       TIMESTAMPTZ,
    last_decision_at   TIMESTAMPTZ,
    last_action        decision_action,
    last_reason        TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);

-- An audit row for EVERY decision, including the boring ones. This is what
-- answers "why didn't it run last night?" -- the single most common question
-- about a nightly, and unanswerable from run rows alone, because the whole
-- point is that no run was created.
CREATE TABLE IF NOT EXISTS scheduler_decision (
    decision_id   BIGSERIAL PRIMARY KEY,
    device_id     TEXT NOT NULL,
    decided_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    action        decision_action NOT NULL,
    reason        TEXT NOT NULL,   -- not_due|already_benchmarked|image_not_ready|...
    build_number  INTEGER,
    run_id        BIGINT REFERENCES run (run_id) ON DELETE SET NULL,
    detail        JSONB
);

CREATE INDEX IF NOT EXISTS decision_device_time_idx
    ON scheduler_decision (device_id, decided_at DESC);


-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

-- Newest run per device, whatever its state. Drives the dashboard index.
CREATE OR REPLACE VIEW run_latest AS
SELECT DISTINCT ON (device_id) *
FROM run
ORDER BY device_id, queued_at DESC, run_id DESC;

-- The like-for-like trend series: one row per (run, phase, boot_index),
-- carrying enough run context to filter honestly. Consumers should pin
-- phase and boot_index -- see the rmtfs note on boot.phase -- and will
-- usually also want `reflashed IS NOT FALSE` and
-- `cmdline_has_debug IS NOT TRUE` on a default-phase series.
CREATE OR REPLACE VIEW boot_trend AS
SELECT
    b.boot_id,
    b.run_id,
    r.device_id,
    r.target,
    r.build_number,
    r.build_folder,
    r.status,
    r.reflashed,
    r.started_at,
    r.finished_at,
    b.log_name,
    b.phase,
    b.boot_index,
    b.cmdline_has_debug,
    b.nhlos_s,
    b.kernel_s,
    b.initramfs_s,
    b.sysinit_svc_s,
    b.total_sysinit_s,
    b.total_multiuser_s,
    b.grand_total_s,
    b.firmware_s,
    b.loader_s,
    b.userspace_s,
    b.sat_total_s,
    b.init_exec_s,
    b.epoch_advanced_s,
    b.systemd_running_s,
    b.sysinit_target_s,
    b.cc_multiuser_s,
    b.overall_overheads
FROM boot b
JOIN run r USING (run_id)
WHERE b.recorded;


-- ---------------------------------------------------------------------------
-- Migration bookkeeping
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_migration (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sha256     TEXT
);
