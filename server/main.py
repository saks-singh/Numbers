"""bootbench coordinator CLI.

One entry point for every mode: validate config, ping an agent, evaluate
the schedule, run the worker, serve the dashboard, ingest, backfill, gc.

Nothing here touches hardware. The bench host's agent does, and this
process only ever asks it to.

    python main.py validate-config
    python main.py agent-ping --device iq-9075-evk-01
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from src.inventory import InventoryError, load_inventory  # noqa: E402
from src.settings import load_settings  # noqa: E402
from src.utils.logger import setup_logging  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_UNREACHABLE = 4

# Subcommands whose phase has not been built. Empty now that every phase
# that can be built without a board is built; kept because `--help` telling
# the truth about an unimplemented mode is better than a traceback, and the
# next mode to be added goes here first.
PENDING: dict = {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_inventory_or_exit(args):
    settings = load_settings(getattr(args, "config", None))
    path = Path(args.devices) if getattr(args, "devices", None) else \
        settings.resolved_devices_yaml()
    try:
        return settings, load_inventory(path)
    except InventoryError as e:
        print(f"inventory {path} is invalid:", file=sys.stderr)
        for problem in e.problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)


def _table(rows, headers):
    """Plain fixed-width table. No dependency, aligns in a terminal and in a
    cron log."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.upper().ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * w for w in widths)]
    for row in rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# validate-config
# ---------------------------------------------------------------------------

def cmd_validate_config(args) -> int:
    settings, inventory = _load_inventory_or_exit(args)

    print(f"server.yaml:  {settings.source}")
    print(f"devices.yaml: {inventory.source}")
    if settings.unknown_keys:
        # Not fatal, but a typo'd key silently using a default is exactly the
        # kind of thing that looks like a code bug six weeks later.
        print(f"  warning: unrecognized server.yaml keys: "
              f"{', '.join(settings.unknown_keys)}")
    print()

    rows = []
    for device in sorted(inventory, key=lambda d: d.device_id):
        rows.append([
            device.device_id,
            "yes" if device.enabled else "no",
            device.target,
            device.slug,
            device.agent_host,
            device.com_port or "-",
            device.tac_port or "-",
            device.adb_serial or "-",
            device.num_boots,
            device.boots_expected,
            device.boot_timeout,
            device.schedule,
            device.stages,
        ])
    print(_table(rows, [
        "device_id", "on", "target", "slug", "agent", "com", "tac", "adb",
        "boots", "expect", "timeout", "schedule", "stages",
    ]))
    print()

    enabled = inventory.enabled
    print(f"{len(inventory)} device(s) defined, {len(enabled)} enabled, "
          f"{len(inventory.agent_urls)} agent(s): "
          f"{', '.join(inventory.agent_urls)}")

    # Credentials are checked but never required: a dashboard that only reads
    # the database must still start when the SMTP password is absent.
    from src.utils.credential_manager import get_manager

    manager = get_manager()
    missing = []
    for device in enabled:
        if not manager.get_agent_token(device.agent_url):
            missing.append(device.agent_url)
    if missing:
        print()
        print("warning: no agent token resolved for "
              f"{', '.join(sorted(set(missing)))}")
        print("         set BOOTBENCH_AGENT_TOKEN or add agent_token to "
              "config/credentials.yaml")
    if not manager.get_trigger_token():
        print("warning: no trigger_token; the web app will refuse every POST")

    # A nightly that skips the flash starts from an rmtfs-masked system and is
    # not comparable with the run before it. See Risk 2 in the plan.
    for device in enabled:
        if device.stages != "all":
            print(f"warning: {device.device_id} runs stages={device.stages!r}, "
                  "not 'all'; without a reflash its boots are not comparable "
                  "across runs (rmtfs stays masked)")

    # Which bootbench.py this process parses with. Printed because silently
    # using a different vintage of the skill than the bench host runs is a
    # hard problem to spot from the numbers alone -- the failure is a boot
    # time that is wrong by a plausible-looking amount, not an exception.
    from src.utils import bootbench_api

    print()
    try:
        bootbench_api.load(settings.bootbench_path)
        print(f"bootbench.py:  {bootbench_api.source()}")
    except FileNotFoundError as exc:
        print("warning: bootbench.py not found; backfill and log repair are "
              "unavailable (ingesting a status document still works)")
        print(f"         {exc}".replace("\n", "\n         "))

    return EXIT_OK


# ---------------------------------------------------------------------------
# agent-ping
# ---------------------------------------------------------------------------

def cmd_agent_ping(args) -> int:
    from src.runner.agent_client import (
        AgentError, AgentUnreachable, assess_health, client_for,
    )

    _settings, inventory = _load_inventory_or_exit(args)

    if args.device:
        device = inventory.get(args.device)
        if device is None:
            print(f"no such device: {args.device}", file=sys.stderr)
            print(f"known: {', '.join(sorted(inventory.devices))}", file=sys.stderr)
            return EXIT_USAGE
        targets = [device]
    else:
        targets = sorted(inventory, key=lambda d: d.device_id)

    # One agent can host several devices; ping each agent once.
    seen = {}
    for device in targets:
        seen.setdefault(device.agent_url, device)

    worst = EXIT_OK
    for agent_url, device in seen.items():
        client = client_for(device)
        print(f"== {agent_url}")
        try:
            health = client.healthz()
        except AgentUnreachable as e:
            print(f"   UNREACHABLE: {e}")
            worst = max(worst, EXIT_UNREACHABLE)
            continue
        except AgentError as e:
            print(f"   ERROR: {e}")
            worst = max(worst, EXIT_ERROR)
            continue

        if args.json:
            print(json.dumps(health, indent=2, sort_keys=True))
            continue

        ok, reasons = assess_health(health)
        print(f"   agent        {health.get('agent_version')} on "
              f"{health.get('hostname')} (python {health.get('python')})")
        print(f"   session      {health.get('session_id')} "
              f"interactive={health.get('session_interactive')}")
        print(f"   tac          count={health.get('tac_device_count')} "
              f"ports={health.get('tac_ports')} "
              f"error={health.get('tac_error')}")
        print(f"   pcat         present={health.get('pcat_present')}")
        print(f"   adb          {health.get('adb_devices')}")
        print(f"   devices      {health.get('devices')}")
        print(f"   busy         {health.get('busy')}")
        print(f"   free         {health.get('artifact_root_free_gb')} GB")
        print(f"   VERDICT      {'healthy' if ok else 'UNHEALTHY'}")
        for reason in reasons:
            print(f"                - {reason}")
        if not ok:
            worst = max(worst, EXIT_ERROR)

        # The inventory promises these devices exist on this agent; the agent
        # refuses any device_id it does not own, so a mismatch is a config bug
        # that would otherwise surface as a 404 at 01:00.
        owned = set(health.get("devices") or [])
        expected = {d.device_id for d in targets if d.agent_url == agent_url}
        orphans = sorted(expected - owned)
        if orphans:
            print(f"   MISMATCH     agent does not own: {', '.join(orphans)}")
            worst = max(worst, EXIT_ERROR)

    return worst


# ---------------------------------------------------------------------------
# agent-latest-build  (cheap, device-free; handy before Phase 8 exists)
# ---------------------------------------------------------------------------

def cmd_latest_build(args) -> int:
    from src.runner.agent_client import AgentError, client_for

    _settings, inventory = _load_inventory_or_exit(args)
    device = inventory.get(args.device)
    if device is None:
        print(f"no such device: {args.device}", file=sys.stderr)
        return EXIT_USAGE

    try:
        payload = client_for(device).latest_build(device.device_id)
    except AgentError as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_UNREACHABLE

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK

    build = payload.get("build") or payload
    for key in ("share_root", "build_number", "build_folder", "build_path",
                "performance_path", "target", "build_dir", "target_image_ready"):
        if key in build:
            print(f"{key:20} {build[key]}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# initdb
# ---------------------------------------------------------------------------

def cmd_initdb(args) -> int:
    """Apply migrations, then project the inventory into the device table.

    Expected to be run repeatedly -- on a fresh database, after a pull, and
    by someone who is not sure whether they already ran it. Both the
    bookkeeping and the DDL itself are idempotent, so a second run prints
    'skipped' rather than failing.
    """
    from src.db import migrate, pool, queries

    if args.dsn is not None:
        pool.set_dsn(args.dsn)

    problems = migrate.validate_names()
    if problems:
        for problem in problems:
            print(f"bad migration name: {problem}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        with pool.connection() as conn:
            actions = migrate.apply_migrations(conn, dry_run=args.dry_run)
            state = migrate.status(conn)

            devices_projected = None
            if not args.dry_run and not args.no_devices:
                # Projected here so Phase 5's backfill has a device row to
                # point its foreign key at. Harmless to repeat: it is an
                # upsert, and first_seen_at is never overwritten.
                _settings, inventory = _load_inventory_or_exit(args)
                devices_projected = queries.upsert_inventory(conn, inventory)
                conn.commit()
    except pool.DatabaseError as e:
        print(f"{e}", file=sys.stderr)
        print("hint: the DSN is empty by default, which lets libpq read "
              "PGHOST/PGUSER/PGDATABASE and ~/.pgpass. Set BOOTBENCH_DB_DSN "
              "or db_dsn in config/credentials.yaml to override.",
              file=sys.stderr)
        return EXIT_CONFIG
    except migrate.MigrationError as e:
        print(f"migration failed: {e}", file=sys.stderr)
        return EXIT_ERROR

    for name, action, detail in actions:
        print(f"{action:8} {name}  ({detail})")
    print()
    print(f"applied  {len(state['applied'])}: {', '.join(state['applied'])}")
    if state["pending"]:
        print(f"pending  {', '.join(state['pending'])}")
    if state["changed"]:
        # Not fatal during development, but it means this database never ran
        # what the file now says -- and that divergence stays invisible until
        # a query fails.
        print(f"CHANGED  {', '.join(state['changed'])} -- this database did "
              f"not run the current file contents")
    if state["orphaned"]:
        print(f"orphaned {', '.join(state['orphaned'])} -- recorded but the "
              f"file is gone")
    if devices_projected is not None:
        print(f"devices  {devices_projected} projected from "
              f"{Path(args.devices).name if args.devices else 'devices.yaml'}")

    return EXIT_ERROR if state["changed"] else EXIT_OK


# ---------------------------------------------------------------------------
# ingest / backfill
# ---------------------------------------------------------------------------

def _device_or_exit(args, device_id):
    _settings, inventory = _load_inventory_or_exit(args)
    device = inventory.devices.get(device_id)
    if device is None:
        known = ", ".join(sorted(inventory.devices)) or "(none)"
        print(f"unknown device '{device_id}'. Known: {known}",
              file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return device


def cmd_ingest(args) -> int:
    """Ingest one --json-status document into an existing run.

    The repair hatch for a run the worker could not finish ingesting -- and,
    before the worker exists, the way to put a hand-run job's status document
    into the database.
    """
    import json

    from src.db import ingest, pool, queries

    if args.dsn is not None:
        pool.set_dsn(args.dsn)

    path = Path(args.status)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return EXIT_USAGE

    try:
        with pool.connection() as conn:
            if args.run_id:
                run_id = args.run_id
                if queries.run_detail(conn, run_id) is None:
                    print(f"no run {run_id}", file=sys.stderr)
                    return EXIT_USAGE
            else:
                # No run row yet: this is a job nobody enqueued, which is
                # exactly the case for a status document produced by a
                # hand-run bootbench invocation.
                device = _device_or_exit(args, args.device)
                queries.upsert_device(conn, device)
                run_id = queries.enqueue_run(
                    conn, device, trigger_source="manual",
                    triggered_by=args.triggered_by or "ingest-cli")
                print(f"created run {run_id} for {device.device_id}")

            summary = ingest.ingest_status(conn, run_id, doc)
            queries.finish_run(conn, run_id, summary["status"],
                               finished_at=ingest._utc(doc.get("ended_utc")))
            if args.dry_run:
                conn.rollback()
                print("(dry run -- rolled back)")
            else:
                conn.commit()
    except pool.DatabaseError as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_CONFIG
    except ingest.IngestError as e:
        print(f"ingest failed: {e}", file=sys.stderr)
        return EXIT_ERROR

    print(f"run {summary['run_id']}: {summary['status']}, "
          f"{summary['recorded']}/{summary['boots']} boot(s) recorded")
    for name in summary["parse_errors"]:
        print(f"  parse error: {name}")
    return EXIT_OK


def cmd_backfill(args) -> int:
    """Load history out of the skill's own bootchart-data-<slug>.json.

    Worth running before any orchestration exists: it turns the runs you
    already have into queryable SQL. Two limits are inherent to the source
    and not worth hiding -- the file holds only the 2 displayed boots of
    each run, and its timestamps are naive local minute stamps, so runs
    order by build number rather than by a real clock.
    """
    from src.db import ingest, pool, queries

    if args.dsn is not None:
        pool.set_dsn(args.dsn)

    device = _device_or_exit(args, args.device)

    path = Path(args.json) if args.json else None
    if path is None:
        print("--json is required: point it at the "
              f"bootchart-data-{device.slug}.json the skill maintains",
              file=sys.stderr)
        return EXIT_USAGE
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return EXIT_USAGE

    try:
        with pool.connection() as conn:
            queries.upsert_device(conn, device)
            results = ingest.ingest_from_target_json(
                conn, device.device_id, path,
                target=device.target, limit=args.limit)
            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()
    except pool.DatabaseError as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_CONFIG
    except ingest.IngestError as e:
        print(f"backfill failed: {e}", file=sys.stderr)
        return EXIT_ERROR

    added = [r for r in results if r.get("run_id")]
    skipped = [r for r in results if not r.get("run_id")]
    for result in added:
        print(f"run {result['run_id']:>5}  {result['status']:8} "
              f"{result['recorded']}/{result['boots']} boot(s)  "
              f"{result.get('bootbench_ts') or ''}")
    print()
    print(f"{len(added)} run(s) ingested, {len(skipped)} already present")
    if args.dry_run:
        print("(dry run -- rolled back)")
    return EXIT_OK


# ---------------------------------------------------------------------------
# tick / run-nightly  (the scheduler)
# ---------------------------------------------------------------------------

def _runner_factory(settings, inventory):
    """How `tick` and `worker` reach a bench host.

    `local_fake` is refused here rather than silently accepted: a
    coordinator configured that way would report healthy nightlies while
    benchmarking nothing, which is the one failure mode this whole system
    exists to prevent. Tests inject their own factory.
    """
    from src.runner.agent_client import client_for

    if settings.runner_backend == "local_fake":
        print("runner_backend is 'local_fake'; that backend is for tests "
              "only. Set runner_backend: agent in server.yaml.",
              file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)
    return client_for


def cmd_tick(args) -> int:
    from src.db import pool, queries
    from src.scheduler import nightly, notify

    settings, inventory = _load_inventory_or_exit(args)
    only = None
    if args.device:
        _device_or_exit(args, args.device)
        only = {args.device}

    runner_for = _runner_factory(settings, inventory)

    try:
        with pool.connection() as conn:
            queries.upsert_inventory(conn, inventory)
            decisions = nightly.tick(
                conn, inventory, settings=settings, runner_for=runner_for,
                force=args.force, dry_run=args.dry_run, only=only)
            summary = (notify.evaluate(conn, inventory, settings, decisions,
                                       dry_run=args.dry_run)
                       if not args.no_notify else None)
            if args.dry_run:
                conn.rollback()
            else:
                conn.commit()
    except pool.DatabaseError as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_CONFIG

    rows = [["DEVICE", "ACTION", "REASON", "BUILD", "RUN"]]
    for decision in decisions:
        rows.append([
            decision.device_id, decision.action, decision.reason,
            str((decision.build or {}).get("build_number") or "-"),
            str(decision.run_id or "-"),
        ])
    print(_table(rows))

    if summary and summary["alerts"]:
        print()
        for alert in summary["alerts"]:
            state = ("muted" if alert["key"] in summary["muted"]
                     else "sent" if alert["key"] in summary["sent"]
                     else "FAILED")
            print(f"alert {state:6} {alert['key']}: {alert['subject']}")

    if args.dry_run:
        print("\n(dry run -- nothing enqueued, nothing recorded)")

    # A tick that could not evaluate a device is not a failed tick: the next
    # one in fifteen minutes retries for free, and exiting non-zero would
    # have cron mail someone every quarter hour about a logged-out bench host
    # the dashboard already shows.
    return EXIT_OK


def cmd_run_nightly(args) -> int:
    """One device, now, bypassing the schedule.

    `tick --force --device X` does the same thing; this exists because
    "run the nightly for this board" is what someone actually wants to type,
    and because it is the natural thing to put in a one-off at(1) job.
    """
    if not args.device:
        print("--device is required", file=sys.stderr)
        return EXIT_USAGE
    args.force = True
    args.no_notify = getattr(args, "no_notify", False)
    return cmd_tick(args)


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------

def cmd_worker(args) -> int:
    from src.db import pool
    from src.jobs import worker as worker_module

    settings, inventory = _load_inventory_or_exit(args)
    _runner_factory(settings, inventory)  # fail fast on a test-only backend

    max_runs = 1 if args.once else args.max_runs
    try:
        executed = worker_module.run_worker(settings, inventory,
                                            max_runs=max_runs)
    except pool.DatabaseError as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        # Ctrl-C does not abandon the job on the bench host: it keeps running
        # and the next worker re-adopts it by polling the same job id.
        print("\nworker stopped; any job in flight continues on the agent "
              "and will be picked up again", file=sys.stderr)
        return EXIT_OK

    if args.once and not executed:
        print("nothing queued")
    return EXIT_OK


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

def cmd_serve(args) -> int:
    """Development server. Production is waitress via the systemd unit.

    Deliberately not Flask's own `app.run()` with the reloader: a reloader
    that restarts on a file write is fine for templates and surprising for
    anything else, and the unit file uses waitress anyway -- so this path
    runs the same WSGI server, just started by hand.
    """
    from src.web.app import create_app

    settings, _inventory = _load_inventory_or_exit(args)
    app = create_app(config_path=args.config,
                     devices_path=getattr(args, "devices", None),
                     settings=settings)

    host = args.bind or settings.bind
    port = args.port or settings.port
    print(f"bootbench dashboard on http://{host}:{port}/  (Ctrl-C to stop)")
    print("this host is not internet-facing; see the README's threat note")

    try:
        from waitress import serve as waitress_serve
    except ImportError:
        print("waitress is not installed; falling back to Flask's "
              "development server", file=sys.stderr)
        app.run(host=host, port=port, debug=False, use_reloader=False)
        return EXIT_OK

    waitress_serve(app, host=host, port=port, threads=args.threads)
    return EXIT_OK


# ---------------------------------------------------------------------------
# gc
# ---------------------------------------------------------------------------

def cmd_gc(args) -> int:
    from src.db import pool
    from src.jobs import gc as gc_module

    settings, _inventory = _load_inventory_or_exit(args)
    try:
        summary = gc_module.sweep(
            settings, keep_days=args.keep_days,
            keep_per_device=args.keep_runs_per_device,
            decision_keep_days=args.keep_decision_days,
            dry_run=args.dry_run)
    except pool.DatabaseError as e:
        print(f"{e}", file=sys.stderr)
        return EXIT_CONFIG

    if not summary["runs"]:
        print(f"nothing to prune (keep_days={summary['keep_days']}, "
              f"keep_runs_per_device={summary['keep_per_device']})")
        return EXIT_OK

    rows = [["RUN", "DEVICE", "BUILD", "STATUS", "MB"]]
    for run in summary["runs"]:
        rows.append([
            str(run["run_id"]), run["device_id"],
            str(run.get("build_number") or "-"), run.get("status") or "-",
            f"{run['bytes'] / 1_048_576:.1f}",
        ])
    print(_table(rows))
    print()
    print(f"{summary['deleted']} run(s), {summary['dirs']} directory tree(s), "
          f"{summary['bytes'] / 1_048_576:.1f} MB, "
          f"{summary['decisions']} decision row(s)")
    if args.dry_run:
        print("(dry run -- nothing deleted)")
    return EXIT_OK


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="path to server.yaml")
    parser.add_argument("--devices", help="path to devices.yaml")
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate-config",
                   help="parse server.yaml + devices.yaml and print the "
                        "resolved device table")

    ping = sub.add_parser("agent-ping", help="GET /healthz on each agent")
    ping.add_argument("--device", help="only this device's agent")
    ping.add_argument("--json", action="store_true", help="raw payload")

    latest = sub.add_parser("latest-build",
                            help="ask an agent what the newest build is "
                                 "(no hardware touched)")
    latest.add_argument("--device", required=True)
    latest.add_argument("--json", action="store_true")

    init = sub.add_parser("initdb",
                          help="apply migrations and project devices.yaml "
                               "into the device table (idempotent)")
    init.add_argument("--dsn", default=None,
                      help="override the resolved DSN; '' means let libpq "
                           "decide from PGHOST/PGUSER/~/.pgpass")
    init.add_argument("--dry-run", action="store_true",
                      help="report what would be applied, change nothing")
    init.add_argument("--no-devices", action="store_true",
                      help="migrations only; skip the device projection")

    ing = sub.add_parser("ingest",
                         help="load one --json-status document into a run")
    ing.add_argument("--status", required=True,
                     help="path to the status.json bootbench wrote")
    ing.add_argument("--run-id", type=int,
                     help="ingest into this existing run; omit to create one")
    ing.add_argument("--device",
                     help="device id, required when --run-id is omitted")
    ing.add_argument("--triggered-by", help="recorded on a created run")
    ing.add_argument("--dsn", default=None)
    ing.add_argument("--dry-run", action="store_true",
                     help="parse and write, then roll back")

    back = sub.add_parser("backfill",
                          help="load history from the skill's own "
                               "bootchart-data-<slug>.json")
    back.add_argument("--device", required=True)
    back.add_argument("--json", help="path to bootchart-data-<slug>.json")
    back.add_argument("--limit", type=int,
                      help="only the newest N entries in the file")
    back.add_argument("--dsn", default=None)
    back.add_argument("--dry-run", action="store_true",
                      help="report what would be ingested, change nothing")

    tick = sub.add_parser("tick",
                          help="evaluate every device's schedule and enqueue "
                               "what is due (the crontab entry)")
    tick.add_argument("--device", help="only this device")
    tick.add_argument("--force", action="store_true",
                      help="ignore the schedule and the already-benchmarked "
                           "check; still refuses to overlap a live run")
    tick.add_argument("--dry-run", action="store_true",
                      help="decide and print, enqueue nothing, record nothing")
    tick.add_argument("--no-notify", action="store_true",
                      help="skip alerting for this tick")

    nightly_cmd = sub.add_parser(
        "run-nightly", help="run one device now, bypassing its schedule")
    nightly_cmd.add_argument("--device", required=True)
    nightly_cmd.add_argument("--dry-run", action="store_true")
    nightly_cmd.add_argument("--no-notify", action="store_true")

    work = sub.add_parser("worker",
                          help="claim queued runs and drive them on their "
                               "agents (the long-running service)")
    work.add_argument("--once", action="store_true",
                      help="claim at most one run, then exit")
    work.add_argument("--max-runs", type=int, default=None,
                      help="exit after this many runs")

    web = sub.add_parser("serve", help="run the dashboard")
    web.add_argument("--bind", default=None, help="override server.yaml")
    web.add_argument("--port", type=int, default=None)
    web.add_argument("--threads", type=int, default=6)

    collect = sub.add_parser(
        "gc", help="delete aged runs, their mirrored artifacts, and old "
                   "decision rows")
    collect.add_argument("--keep-days", type=int, default=None,
                         help="override server.yaml keep_days")
    collect.add_argument("--keep-runs-per-device", type=int, default=None,
                         help="override server.yaml keep_runs_per_device")
    collect.add_argument("--keep-decision-days", type=int, default=None,
                         help="decision rows older than this go too "
                              "(default: twice keep_days, minimum a year)")
    collect.add_argument("--dry-run", action="store_true",
                         help="list what would be deleted, delete nothing")

    for name, phase in sorted(PENDING.items()):
        sub.add_parser(name, help=f"not implemented yet -- arrives in {phase}")

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    settings = load_settings(args.config)
    setup_logging(args.log_level or settings.log_level, settings.log_file or None)

    if args.command in PENDING:
        print(f"`{args.command}` is not implemented yet: "
              f"{PENDING[args.command]}", file=sys.stderr)
        return EXIT_USAGE

    handlers = {
        "validate-config": cmd_validate_config,
        "agent-ping": cmd_agent_ping,
        "latest-build": cmd_latest_build,
        "initdb": cmd_initdb,
        "ingest": cmd_ingest,
        "backfill": cmd_backfill,
        "tick": cmd_tick,
        "run-nightly": cmd_run_nightly,
        "worker": cmd_worker,
        "serve": cmd_serve,
        "gc": cmd_gc,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
