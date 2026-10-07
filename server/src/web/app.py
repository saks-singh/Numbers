"""The Flask application factory.

`create_app()` rather than a module-level `app` because that is what
waitress's `--call` wants:

    waitress-serve --listen=127.0.0.1:8080 --call main:create_app

Two things are deliberately loaded once, at startup, and held in
`app.config`:

  * settings, because server.yaml changing under a running process is more
    confusing than a restart;
  * the inventory, because every page needs it and re-reading devices.yaml
    per request would turn a YAML typo into a 500 on every route rather
    than a clear failure at boot.

`devices.yaml` is re-read when its mtime changes, so adding a device needs
no restart -- but a parse error after boot leaves the last good inventory
in place and logs loudly, which is the right direction to fail in for a
dashboard whose job is to tell you something is wrong.

The app never writes to a bench host and never does long work in a request.
Restarting it cannot disturb a forty-minute flash, because the worker is a
separate process.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, render_template, request

from ..inventory import InventoryError, load_inventory
from ..settings import load_settings
from ..utils.logger import get_logger, setup_logging
from . import charts
from .views import bp

log = get_logger("web")

TEMPLATES = Path(__file__).resolve().parent / "templates"


class _InventoryHolder:
    """The loaded inventory, reloaded when devices.yaml changes on disk.

    Quacks like an `Inventory` for the handful of operations the views use,
    so nothing downstream has to know the file can change.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._mtime = None
        self._inventory = None
        self.reload()

    def reload(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._inventory is not None and mtime == self._mtime:
            return self._inventory
        try:
            self._inventory = load_inventory(self.path)
            self._mtime = mtime
            log.info("inventory: %s device(s) from %s",
                     len(self._inventory), self.path)
        except InventoryError as exc:
            if self._inventory is None:
                raise
            # Keep serving the last good inventory. A dashboard that 500s
            # because someone mistyped a COM port is worse than a stale one
            # that says so on /healthz.
            log.error("devices.yaml did not parse; keeping the previous "
                      "inventory: %s", exc)
            self._mtime = mtime
        return self._inventory

    # -- the Inventory surface the views use -----------------------------
    def __iter__(self):
        return iter(self.reload())

    def __len__(self):
        return len(self.reload())

    def __getitem__(self, device_id):
        return self.reload()[device_id]

    def get(self, device_id):
        return self.reload().get(device_id)

    @property
    def enabled(self):
        return self.reload().enabled

    @property
    def source(self):
        return self.reload().source


def create_app(config_path=None, devices_path=None, settings=None,
               inventory=None) -> Flask:
    settings = settings or load_settings(config_path)
    setup_logging(settings.log_level, settings.log_file or None)

    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["BOOTBENCH_SETTINGS"] = settings
    app.config["BOOTBENCH_INVENTORY"] = (
        inventory if inventory is not None
        else _InventoryHolder(devices_path or settings.resolved_devices_yaml())
    )
    # No session, no flash messages, no cookies: there is nothing to sign.
    app.config["JSON_SORT_KEYS"] = False
    app.register_blueprint(bp)

    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    app.jinja_env.filters["seconds"] = charts.seconds
    app.jinja_env.filters["stamp"] = _stamp
    app.jinja_env.filters["ago"] = _ago
    app.jinja_env.filters["filesize"] = _filesize
    app.jinja_env.filters["status_class"] = charts.status_class
    app.jinja_env.filters["action_class"] = charts.action_class

    @app.errorhandler(404)
    def _not_found(exc):
        if _wants_html():
            return render_template("error.html", code=404,
                                   message=_describe(exc)), 404
        return {"error": _describe(exc)}, 404

    @app.errorhandler(401)
    def _unauthorized(exc):
        if _wants_html():
            return render_template("error.html", code=401,
                                   message=_describe(exc)), 401
        return {"error": _describe(exc)}, 401

    @app.errorhandler(500)
    def _server_error(exc):  # pragma: no cover - defensive
        log.exception("unhandled error on %s", request.path)
        if _wants_html():
            return render_template(
                "error.html", code=500,
                message="Something failed server-side; see the server log "
                        "and /healthz."), 500
        return {"error": "internal error"}, 500

    return app


def _wants_html() -> bool:
    return "text/html" in (request.headers.get("Accept") or "") \
        and not request.path.startswith("/api/")


def _describe(exc) -> str:
    return getattr(exc, "description", None) or str(exc)


# ---------------------------------------------------------------------------
# template filters
# ---------------------------------------------------------------------------

def _stamp(value, fmt="%Y-%m-%d %H:%M") -> str:
    if value is None:
        return "—"
    try:
        return value.strftime(fmt)
    except AttributeError:
        return str(value)


def _ago(value) -> str:
    """Coarse relative time. Minutes, hours, days -- never "just now",
    which reads as a bug when a page is left open."""
    from datetime import datetime, timezone
    if value is None:
        return "—"
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - value
    except (AttributeError, TypeError):
        return str(value)
    total = int(delta.total_seconds())
    if total < 0:
        return "—"
    if total < 120:
        return f"{total}s ago"
    if total < 7200:
        return f"{total // 60}m ago"
    if total < 172800:
        return f"{total // 3600}h ago"
    return f"{total // 86400}d ago"


def _filesize(value) -> str:
    """Bytes as KB/MB/GB. Enough precision to tell a truncated log from a
    complete one, not enough to invite arithmetic."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "—"
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GB"
