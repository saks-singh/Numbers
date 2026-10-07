"""Server settings: config/server.yaml plus defaults.

Separate from `credential_manager` because these are not secrets and the
failure modes differ: a missing credential is a warning the consumer
decides about, whereas a missing server.yaml is fine -- every key has a
default, so a fresh checkout runs.

Scalar-only and flat, because the stdlib fallback YAML parser cannot read
lists and silently truncates anything after a '#'.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from .utils.config_loader import load_yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DEFAULT_SERVER_YAML = CONFIG_DIR / "server.yaml"
DEFAULT_DEVICES_YAML = CONFIG_DIR / "devices.yaml"


@dataclass
class Settings:
    artifact_root: str = str(ROOT / "artifacts")
    log_file: str = ""
    log_level: str = "INFO"
    runner_backend: str = "agent"
    poll_interval: int = 5
    claim_interval: int = 10
    agent_backoff_max: int = 120
    bind: str = "127.0.0.1"
    port: int = 8080
    keep_days: int = 90
    keep_runs_per_device: int = 400
    timezone: str = "Asia/Kolkata"
    retry_delay_minutes: int = 30
    stale_success_days: int = 3

    # Where the coordinator finds bootbench.py, whose parsers it reuses for
    # backfill and repair. Empty means search the usual relative locations --
    # see src/utils/bootbench_api.py.
    bootbench_path: str = ""

    source: str = ""
    unknown_keys: tuple = ()

    @property
    def artifact_path(self) -> Path:
        return Path(self.artifact_root)

    def run_dir(self, run_id) -> Path:
        """Coordinator-side mirror of one run. Holds runner.log, status.json,
        and the fetched artifact tree -- so /runs/<id>/log survives the agent
        pruning its own job dirs."""
        return self.artifact_path / "runs" / str(run_id)

    def devices_yaml(self) -> Path:
        return DEFAULT_DEVICES_YAML

    def resolved_devices_yaml(self) -> Path:
        """devices.yaml if present, else the shipped example. Running against
        the example is better than a traceback on a fresh checkout, and
        validate-config prints which file it read."""
        if DEFAULT_DEVICES_YAML.is_file():
            return DEFAULT_DEVICES_YAML
        return CONFIG_DIR / "devices.yaml.example"


_INT_FIELDS = {
    f.name for f in fields(Settings) if f.type in ("int", int)
}


def load_settings(path=None) -> Settings:
    path = Path(path) if path else DEFAULT_SERVER_YAML
    if not path.is_file():
        return Settings(source=f"{path} (absent; using defaults)")

    raw = load_yaml(path) or {}
    known = {f.name for f in fields(Settings)} - {"source", "unknown_keys"}

    values = {}
    unknown = []
    for key, value in raw.items():
        if key not in known:
            unknown.append(key)
            continue
        if key in _INT_FIELDS:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue  # keep the default rather than crash on a typo
        values[key] = value

    return Settings(source=str(path), unknown_keys=tuple(unknown), **values)
