"""Centralized credential access.

Mirrors aibench/src/utils/credential_manager.py: a singleton with
module-level accessors that **never raises**. A missing credential returns
None and the consumer decides whether that is fatal -- so a dashboard that
only reads the database still starts when the SMTP password is absent.

Resolution order per credential: config/credentials.yaml, then the
environment, then a warning and None.

No credential is ever written to a config file that is committed. Ship
credentials.yaml.example; gitignore credentials.yaml.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .config_loader import load_yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "credentials.yaml"

_ENV = {
    "db_dsn": "BOOTBENCH_DB_DSN",
    "trigger_token": "BOOTBENCH_TRIGGER_TOKEN",
    "agent_token": "BOOTBENCH_AGENT_TOKEN",
    "smtp_host": "BOOTBENCH_SMTP_HOST",
    "smtp_port": "BOOTBENCH_SMTP_PORT",
    "smtp_user": "BOOTBENCH_SMTP_USER",
    "smtp_password": "BOOTBENCH_SMTP_PASSWORD",
    "smtp_from": "BOOTBENCH_SMTP_FROM",
    "notify_to": "BOOTBENCH_NOTIFY_TO",
    "notify_webhook": "BOOTBENCH_NOTIFY_WEBHOOK",
}


def _host_suffix(agent_url: str) -> str:
    """bench-win-01:8765 -> __BENCH_WIN_01, so one coordinator can hold a
    different token per bench host."""
    import urllib.parse

    host = urllib.parse.urlparse(agent_url).hostname or agent_url
    return "__" + "".join(c if c.isalnum() else "_" for c in host).upper()


class CredentialManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self, path: Path | None = None):
        if getattr(self, "_initialized", False) and path is None:
            return
        self.path = Path(path) if path else DEFAULT_PATH
        self.warnings: list = []
        self._data: dict = {}
        if self.path.is_file():
            try:
                self._data = load_yaml(self.path) or {}
            except Exception as e:
                self.warnings.append(f"could not read {self.path}: {e}")
        self._initialized = True

    # -- generic --------------------------------------------------------
    def get(self, key: str, default=None):
        value = self._data.get(key)
        if value not in (None, ""):
            return value
        env_name = _ENV.get(key, "BOOTBENCH_" + key.upper())
        value = os.environ.get(env_name)
        if value not in (None, ""):
            return value
        if default is None:
            self.warnings.append(
                f"no value for {key!r} (looked in {self.path.name} and ${env_name})"
            )
        return default

    # -- specific -------------------------------------------------------
    def get_db_dsn(self):
        """A None DSN is valid and in fact preferred: pass an empty DSN and
        let libpq read PGHOST/PGUSER/~/.pgpass, which keeps the password out
        of our process entirely."""
        return self.get("db_dsn", default="")

    def get_trigger_token(self):
        return self.get("trigger_token")

    def get_agent_token(self, agent_url: str | None = None):
        """Per-host token if one is configured, otherwise the shared one."""
        if agent_url:
            suffix = _host_suffix(agent_url)
            scoped = self._data.get("agent_token" + suffix.lower())
            if scoped:
                return scoped
            scoped = os.environ.get(_ENV["agent_token"] + suffix)
            if scoped:
                return scoped
        return self.get("agent_token")

    def get_notify_credentials(self) -> dict:
        return {
            "smtp_host": self.get("smtp_host", default=""),
            "smtp_port": int(self.get("smtp_port", default=25) or 25),
            "smtp_user": self.get("smtp_user", default=""),
            "smtp_password": self.get("smtp_password", default=""),
            "smtp_from": self.get("smtp_from", default=""),
            # Comma-separated, not a YAML list: the stdlib fallback parser
            # cannot read lists, and credentials are the last file you want
            # failing to load for a subtle reason.
            "notify_to": [
                addr.strip()
                for addr in str(self.get("notify_to", default="")).split(",")
                if addr.strip()
            ],
            "notify_webhook": self.get("notify_webhook", default=""),
        }

    def has_credentials(self) -> bool:
        return bool(self._data) or any(os.environ.get(v) for v in _ENV.values())


_manager = None


def get_manager(path: Path | None = None) -> CredentialManager:
    global _manager
    if _manager is None or path is not None:
        _manager = CredentialManager(path)
    return _manager


def get_db_dsn():
    return get_manager().get_db_dsn()


def get_trigger_token():
    return get_manager().get_trigger_token()


def get_agent_token(agent_url: str | None = None):
    return get_manager().get_agent_token(agent_url)


def get_notify_credentials():
    return get_manager().get_notify_credentials()


def has_credentials():
    return get_manager().has_credentials()
