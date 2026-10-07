"""Routes. Thin on purpose.

Every handler does three things at most: one query, one shaping call into
`charts`, one render. There is no long work in the request path -- the
worker is a separate process, so the most expensive thing the web app ever
does is a SELECT and a `send_file`. That is what lets waitress run this
synchronously with a handful of threads and no queue.

The POST endpoints are the sharp ones, and they are deliberately narrow:

  * `trigger` does a single INSERT inside one transaction that first checks
    for an existing queued-or-running run, and returns in milliseconds. It
    never talks to a bench host. Pressing "Run now" cannot block the page
    on a logged-out Windows box.
  * `cancel` sets a flag. For a `queued` run it finalizes immediately,
    because no worker holds it yet; for a `running` one the worker forwards
    the request to the agent, which honours it only at safe points. The
    agent refuses mid-flash, and the UI says so -- interrupting PCAT
    mid-write is damage, not a cancellation.
  * `acknowledge` clears the `blocked` state after an `unreachable` run.
    That state exists precisely so a human looks at the board, so nothing
    automatic may clear it.

GETs are unauthenticated (read-only, internal network); POSTs require the
shared trigger token. Fail-closed: with no token configured, the dashboard
is readable but not actionable.
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, redirect,
                   render_template, request, send_file, url_for)

from ..db import pool, queries
from ..jobs.worker import HEARTBEAT_NAME
from ..runner.agent_client import AgentError, assess_health, client_for
from ..runner.artifacts import classify
from ..utils.credential_manager import get_trigger_token
from ..utils.logger import get_logger
from . import charts

log = get_logger("web")

bp = Blueprint("bootbench", __name__)

TOKEN_HEADER = "X-Bootbench-Token"

# A run directory holds 12 files per boot plus the report, so ~75 at the
# defaults. The cap is for the pathological case -- a `--resume-pull` that
# mirrored an entire Boot-Logs tree -- so one bad run cannot turn a page
# render into a walk of tens of thousands of files. The byte total is still
# the true one; only the listing is clipped, and the page says so.
FILE_LIST_CAP = 400

# Agent health is proxied through the coordinator so the browser never needs
# the agent's token or a route to the bench host. Cached briefly because the
# device page polls it and `/healthz` on the agent side touches the TAC.
HEALTH_TTL = timedelta(seconds=20)
_health_cache: dict = {}

# A worker that has not written its heartbeat in this long is reported as
# stale. Three claim intervals: one missed write is a slow disk, three is a
# dead process.
HEARTBEAT_STALE = timedelta(seconds=60)


def _utcnow():
    return datetime.now(timezone.utc)


def _settings():
    return current_app.config["BOOTBENCH_SETTINGS"]


def _inventory():
    return current_app.config["BOOTBENCH_INVENTORY"]


def _device_or_404(device_id):
    device = _inventory().get(device_id)
    if device is None:
        abort(404, f"no device {device_id!r} in devices.yaml")
    return device


def _require_token():
    """Fail closed. An unset token means the dashboard is read-only."""
    expected = get_trigger_token()
    if not expected:
        abort(401, "no trigger_token configured; this dashboard is read-only")
    supplied = (request.headers.get(TOKEN_HEADER)
                or request.form.get("token")
                or request.args.get("token")
                or "")
    if not hmac.compare_digest(str(supplied), str(expected)):
        log.warning("rejected %s %s from %s", request.method, request.path,
                    request.remote_addr)
        abort(401, "bad or missing trigger token")


def _json(payload, status=200):
    # json.dumps rather than jsonify so `queries.json_safe` is the only
    # thing standing between a NUMERIC column and a 500: Decimal is not
    # JSON-serializable, and every chart-feeding query casts to float8.
    return Response(json.dumps(payload, default=str), status=status,
                    mimetype="application/json")


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

@bp.get("/")
def index():
    with pool.connection() as conn:
        rows = queries.latest_per_device(conn)
        cards = []
        for row in rows:
            history = queries.trend(conn, row["device_id"], limit=12)
            cards.append(charts.summarize_device(row, history))

    known = {c["device_id"] for c in cards}
    # A device in devices.yaml that the database has never seen. Shown
    # rather than hidden: "I added it and nothing happened" is otherwise
    # indistinguishable from "it is working and has no runs yet".
    pending = [
        {"device_id": d.device_id, "target": d.target,
         "agent_host": d.agent_host, "enabled": d.enabled,
         "notes": d.notes, "status": None,
         "status_class": "muted", "value_text": "—",
         "delta": charts.delta(None, None), "sparkline": "",
         "duration": "—", "run_id": None, "build_number": None,
         "started_at": None, "previous_build": None}
        for d in sorted(_inventory(), key=lambda d: d.device_id)
        if d.device_id not in known
    ]
    return render_template("index.html", cards=cards + pending,
                           token_required=bool(get_trigger_token()))


@bp.get("/devices/<device_id>")
def device(device_id):
    _device_or_404(device_id)
    phase = request.args.get("phase", "default")
    boot_index = _int_arg("boot", 1)

    with pool.connection() as conn:
        rows = queries.latest_per_device(conn)
        row = next((r for r in rows if r["device_id"] == device_id), None)
        history = queries.trend(conn, device_id, phase=phase,
                                boot_index=boot_index, limit=60)
        runs = queries.recent_runs(conn, device_id, limit=40)
        decisions = queries.recent_decisions(conn, device_id, limit=20)

    summary = charts.summarize_device(row or {"device_id": device_id}, history)
    series = charts.series(history)
    return render_template(
        "device.html", device=_inventory()[device_id], summary=summary,
        runs=runs, decisions=decisions, phase=phase, boot_index=boot_index,
        series=series, geometry=charts.plot(series),
        label=f"multi-user.target by build for {device_id}",
        metrics=charts.TABLE_METRICS,
        token_required=bool(get_trigger_token()),
    )


@bp.get("/runs")
def runs():
    """The log inventory: every run, with what was mirrored for it.

    This is the page that answers "where did last Tuesday's logs go?". The
    per-run file count and byte total are walked from disk rather than read
    from a column, because the files are the record of what actually
    arrived -- a stored count would keep claiming 71 files after `gc`
    unlinked them, and a dead link is worse than no link.
    """
    device_id = request.args.get("device") or None
    status = request.args.get("status") or None
    limit = max(1, min(_int_arg("limit", 100), 1000))

    with pool.connection() as conn:
        rows = queries.all_runs(conn, device_id=device_id, status=status,
                                limit=limit)
        counts = queries.run_counts(conn)

    rows = [dict(row, mirror=_mirror_summary(row["run_id"])) for row in rows]
    return render_template(
        "runs.html", runs=rows, counts=counts, selected=device_id,
        status=status, limit=limit,
        total=sum(counts.values()),
        mirrored_bytes=sum(r["mirror"]["bytes"] for r in rows),
        statuses=queries.ALL_STATUSES,
        devices=sorted(_inventory(), key=lambda d: d.device_id),
    )


@bp.get("/runs/<int:run_id>")
def run(run_id):
    with pool.connection() as conn:
        detail = queries.run_detail(conn, run_id)
        if detail is None:
            abort(404, f"no run {run_id}")
        boots = queries.boots_for_run(conn, run_id)

    log_size = _log_size(run_id)
    return render_template(
        "run.html", run=detail, boots=boots, metrics=charts.TABLE_METRICS,
        log_size=log_size, has_report=_report_path(run_id) is not None,
        files=_mirrored_files(run_id),
        active=detail["status"] in queries.ACTIVE_STATUSES,
        token_required=bool(get_trigger_token()),
    )


@bp.get("/runs/<int:run_id>/log")
def run_log(run_id):
    path = _log_path(run_id)
    if not path.is_file():
        return Response("no log mirrored for this run\n", status=404,
                        mimetype="text/plain")
    return send_file(path, mimetype="text/plain", as_attachment=False,
                     download_name=f"run-{run_id}.log")


@bp.get("/runs/<int:run_id>/report")
def run_report(run_id):
    """The skill's own HTML report, served untouched.

    `render_html` produces a self-contained document with inline CSS and no
    external assets, so it needs no rewriting -- which is why this is a
    `send_file` and not a template.
    """
    path = _report_path(run_id)
    if path is None:
        abort(404, "no bootbench report was mirrored for this run")
    return send_file(path, mimetype="text/html")


@bp.get("/runs/<int:run_id>/files/<path:relpath>")
def run_file(run_id, relpath):
    """One mirrored artifact.

    Two deliberate restrictions. The path is resolved and then checked to be
    *under* the run's own directory, so a symlink or a `..` cannot read
    anything else on the coordinator. And only `.txt`/`.log`/`.json` are
    served inline, as `text/plain`; everything else downloads. HTML and SVG
    arrive from a bench host and would otherwise run script on this origin,
    where the trigger token lives in localStorage -- the skill's own report
    is the one exception and has its own route.
    """
    root = _mirror_root(run_id)
    if root is None:
        abort(404, f"nothing mirrored for run {run_id}")
    try:
        target = (root / relpath).resolve()
    except OSError:
        abort(404, "no such artifact")
    if root not in target.parents or not target.is_file():
        abort(404, f"{relpath!r} is not a mirrored artifact of run {run_id}")

    inline = target.suffix.lower() in (".txt", ".log", ".json")
    return send_file(target, mimetype="text/plain" if inline else None,
                     as_attachment=not inline, download_name=target.name)


@bp.get("/devices/<device_id>/report")
def device_report(device_id):
    _device_or_404(device_id)
    with pool.connection() as conn:
        success = queries.last_success(conn, device_id)
    if success is None:
        abort(404, f"{device_id} has no successful run yet")
    return redirect(url_for("bootbench.run_report", run_id=success["run_id"]))


@bp.get("/schedule")
def schedule():
    device_id = request.args.get("device") or None
    with pool.connection() as conn:
        decisions = queries.recent_decisions(conn, device_id, limit=200)
        states = {d.device_id: queries.get_scheduler_state(conn, d.device_id)
                  for d in _inventory()}
    return render_template("schedule.html", decisions=decisions,
                           states=states, selected=device_id,
                           devices=sorted(_inventory(),
                                          key=lambda d: d.device_id))


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

@bp.get("/healthz")
def healthz():
    """Deliberately renders no template.

    This endpoint has to work when Jinja2 is missing, when the database is
    down, and when devices.yaml does not parse -- those are exactly the
    conditions you point a monitor at. So it builds a dict by hand and
    always returns 200-or-503 with a readable body.
    """
    settings = _settings()
    out = {"ok": True, "checks": {}}

    database = pool.check()
    out["checks"]["database"] = database
    if not database.get("ok"):
        out["ok"] = False

    heartbeat = _heartbeat_state(settings)
    out["checks"]["worker"] = heartbeat
    if not heartbeat.get("ok"):
        out["ok"] = False

    try:
        inventory = _inventory()
        out["checks"]["inventory"] = {
            "ok": True, "devices": len(inventory),
            "enabled": len(inventory.enabled),
            "source": str(inventory.source) if inventory.source else "",
        }
    except Exception as exc:  # pragma: no cover - defensive
        out["ok"] = False
        out["checks"]["inventory"] = {"ok": False, "error": str(exc)}

    return _json(out, status=200 if out["ok"] else 503)


def _heartbeat_state(settings) -> dict:
    path = settings.artifact_path / HEARTBEAT_NAME
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return {"ok": False, "error": "no heartbeat file; is the worker "
                                      "running?", "path": str(path)}
    age = _utcnow() - stamp
    return {
        "ok": age < HEARTBEAT_STALE,
        "last_beat": stamp.isoformat(),
        "age_seconds": round(age.total_seconds(), 1),
    }


# ---------------------------------------------------------------------------
# json for the page scripts
# ---------------------------------------------------------------------------

@bp.get("/api/devices/<device_id>/trend")
def api_trend(device_id):
    _device_or_404(device_id)
    phase = request.args.get("phase", "default")
    boot_index = _int_arg("boot", 1)
    limit = max(1, min(_int_arg("limit", 60), 500))

    with pool.connection() as conn:
        rows = queries.trend(conn, device_id, phase=phase,
                             boot_index=boot_index, limit=limit)
    return _json({
        "device_id": device_id, "phase": phase, "boot_index": boot_index,
        "metrics": [key for key, _ in charts.TABLE_METRICS],
        "series": {key: charts.series(rows, key)
                   for key, _ in charts.TABLE_METRICS},
        "rows": queries.json_safe(rows),
    })


@bp.get("/api/devices/<device_id>/agent-health")
def api_agent_health(device_id):
    device = _device_or_404(device_id)
    cached = _health_cache.get(device.agent_url)
    if cached and _utcnow() - cached["at"] < HEALTH_TTL:
        return _json(cached["payload"])

    payload = {"agent_url": device.agent_url}
    try:
        health = client_for(device).healthz()
    except AgentError as exc:
        payload.update({"ok": False, "error": str(exc), "health": None})
    else:
        ok, problems = assess_health(health)
        payload.update({"ok": ok, "problems": problems, "health": health})

    _health_cache[device.agent_url] = {"at": _utcnow(), "payload": payload}
    return _json(payload)


@bp.get("/api/runs/<int:run_id>/log")
def api_run_log(run_id):
    """Byte-offset tail of the mirrored log.

    The same mechanism the coordinator uses against the agent, so there is
    one implementation of "where did I get to" from PCAT's stdout all the
    way to the browser. No SSE and no websocket: a poll of a flushed file
    survives any reverse proxy and needs no extra dependency.
    """
    offset = max(0, _int_arg("offset", 0))
    path = _log_path(run_id)
    if not path.is_file():
        return _json({"offset": 0, "size": 0, "chunk": "", "eof": False})

    size = path.stat().st_size
    offset = min(offset, size)
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()

    with pool.connection() as conn:
        detail = queries.run_detail(conn, run_id)
    status = (detail or {}).get("status")

    return _json({
        "offset": offset + len(data), "size": size,
        "chunk": data.decode("utf-8", errors="replace"),
        "status": status,
        "eof": status not in queries.ACTIVE_STATUSES,
    })


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

@bp.post("/api/devices/<device_id>/trigger")
def api_trigger(device_id):
    _require_token()
    device = _device_or_404(device_id)
    if not device.enabled:
        return _json({"error": f"{device_id} is disabled in devices.yaml"},
                     status=409)

    with pool.connection() as conn:
        active = queries.active_run(conn, device_id)
        if active is not None:
            conn.rollback()
            return _json({"error": "a run is already in flight",
                          "run_id": active["run_id"],
                          "status": active["status"]}, status=409)
        run_id = queries.enqueue_run(
            conn, device, trigger_source="manual",
            triggered_by=request.remote_addr or "web",
            stages=request.form.get("stages") or None,
        )
        conn.commit()

    log.info("manual trigger: %s -> run %s (by %s)", device_id, run_id,
             request.remote_addr)
    if _wants_json():
        return _json({"run_id": run_id, "device_id": device_id,
                      "status": "queued"}, status=202)
    return redirect(url_for("bootbench.run", run_id=run_id), code=303)


@bp.post("/api/runs/<int:run_id>/cancel")
def api_cancel(run_id):
    _require_token()
    with pool.connection() as conn:
        detail = queries.run_detail(conn, run_id)
        if detail is None:
            abort(404, f"no run {run_id}")
        if detail["status"] not in queries.ACTIVE_STATUSES:
            conn.rollback()
            return _json({"error": "run is already finished",
                          "status": detail["status"]}, status=409)

        if detail["status"] == "queued":
            # Nothing has been dispatched, so there is nobody to ask: end it
            # here rather than leaving a cancel flag on a row no worker will
            # ever claim.
            queries.finish_run(conn, run_id, "cancelled",
                               error_class="Cancelled",
                               error_message="cancelled before dispatch")
            outcome = "cancelled"
        else:
            # The worker forwards this to the agent on its next poll. The
            # agent honours it between boots or before the flash begins and
            # refuses mid-flash.
            queries.set_run_fields(conn, run_id, cancel_requested=True)
            outcome = "requested"
        conn.commit()

    if _wants_json():
        return _json({"run_id": run_id, "cancel": outcome})
    return redirect(url_for("bootbench.run", run_id=run_id), code=303)


@bp.post("/api/runs/<int:run_id>/acknowledge")
def api_acknowledge(run_id):
    _require_token()
    with pool.connection() as conn:
        detail = queries.run_detail(conn, run_id)
        if detail is None:
            abort(404, f"no run {run_id}")
        queries.set_run_fields(conn, run_id, acknowledged_at=_utcnow())
        conn.commit()

    log.info("run %s acknowledged by %s", run_id, request.remote_addr)
    if _wants_json():
        return _json({"run_id": run_id, "acknowledged": True})
    return redirect(url_for("bootbench.run", run_id=run_id), code=303)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _wants_json() -> bool:
    """A browser form gets a redirect; curl gets JSON."""
    if request.form.get("redirect") == "0":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept or "text/html" not in accept


def _log_path(run_id) -> Path:
    return _settings().run_dir(run_id) / "runner.log"


def _log_size(run_id) -> int:
    path = _log_path(run_id)
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _report_path(run_id):
    """This run's mirrored bootbench HTML, if it was fetched.

    Resolved by glob rather than by name because the filename carries the
    target's slug, and resolved under the run directory so a `..` in a
    recorded path cannot escape it.
    """
    root = _mirror_root(run_id)
    if root is None:
        return None
    for path in sorted(root.rglob("bootchart-overview-*.html")):
        try:
            if path.is_file() and root in path.resolve().parents:
                return path
        except OSError:
            continue
    return None


def _mirror_root(run_id):
    """The run's mirror directory, resolved, or None if it isn't there."""
    try:
        root = _settings().run_dir(run_id).resolve()
    except OSError:
        return None
    return root if root.is_dir() else None


def _walk_mirror(root):
    """Every regular file under `root`, as `(relpath, size)`, sorted."""
    out = []
    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            # Vanished mid-walk, or a permission problem. One unreadable
            # file must not cost the page the rest of the listing.
            continue
        out.append((path.relative_to(root).as_posix(), size))
    return out


def _mirrored_files(run_id) -> dict:
    """What arrived from the bench host for this run, ready to render.

    Walked from the filesystem, not from a table. The files *are* the
    record: a stored manifest would keep advertising artifacts that `gc`
    has since unlinked, and a link that 404s is worse than no link.
    """
    root = _mirror_root(run_id)
    if root is None:
        return {"root": None, "files": [], "bytes": 0, "count": 0,
                "truncated": 0}

    walked = _walk_mirror(root)
    total = sum(size for _, size in walked)
    shown = walked[:FILE_LIST_CAP]
    files = [{"relpath": relpath, "size": size,
              "kind": classify(relpath),
              "name": relpath.rsplit("/", 1)[-1],
              "dir": relpath.rsplit("/", 1)[0] if "/" in relpath else ""}
             for relpath, size in shown]
    return {"root": str(root), "files": files, "bytes": total,
            "count": len(walked), "truncated": len(walked) - len(shown)}


def _mirror_summary(run_id) -> dict:
    """File count and byte total for the inventory's row.

    One directory walk per rendered row, which is why `/runs` defaults to
    100. Local disk, no network, and it is the only way to report what is
    still actually on the coordinator rather than what was once fetched.
    """
    root = _mirror_root(run_id)
    if root is None:
        return {"count": 0, "bytes": 0, "present": False}
    walked = _walk_mirror(root)
    return {"count": len(walked),
            "bytes": sum(size for _, size in walked),
            "present": bool(walked)}
