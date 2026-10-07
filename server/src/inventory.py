"""Device inventory -- the coordinator's source of truth for what exists.

devices.yaml is a map keyed by device id, not a list of dicts, for three
reasons: the stdlib fallback YAML parser cannot read a list of dicts, the
key is a natural Postgres primary key, and two boards of the same SoC can
coexist (same `target`, different `device_id`).

Adding a device is a new YAML block and nothing else -- no code change, no
crontab change, no migration.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path

from .utils.config_loader import load_yaml

DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

INHERITABLE = (
    "agent_url", "num_boots", "boot_timeout", "schedule", "stages",
    "fetch_full_logs", "max_retries",
)


class InventoryError(Exception):
    """Raised for any invalid inventory. Collects every problem rather than
    the first, so `validate-config` reports a complete list."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def slugify(target: str) -> str:
    """Mirrors bootbench.py's _slugify. The slug selects which
    bootchart-data-<slug>.json a run writes to, so two devices resolving to
    the same slug on one agent would fight over one report file."""
    return target.replace("-", "").replace("_", "")


@dataclass(frozen=True)
class Device:
    device_id: str
    target: str
    agent_url: str
    enabled: bool = True
    com_port: str | None = None
    tac_port: str | None = None
    adb_serial: str | None = None
    num_boots: int = 3
    boot_timeout: int = 480
    schedule: str = "0 1 * * *"
    stages: str = "all"
    fetch_full_logs: bool = False
    max_retries: int = 1
    notes: str = ""

    @property
    def slug(self) -> str:
        return slugify(self.target)

    @property
    def agent_host(self) -> str:
        return urllib.parse.urlparse(self.agent_url).netloc

    @property
    def boots_expected(self) -> int:
        """The capture stage runs num_boots default-cmdline boots and then
        num_boots debug-cmdline boots."""
        return 2 * self.num_boots

    @property
    def is_fully_pinned(self) -> bool:
        return all((self.com_port, self.tac_port, self.adb_serial))


@dataclass
class Inventory:
    devices: dict = field(default_factory=dict)
    source: Path | None = None

    def __iter__(self):
        return iter(self.devices.values())

    def __len__(self):
        return len(self.devices)

    def __getitem__(self, device_id: str) -> Device:
        return self.devices[device_id]

    def get(self, device_id: str) -> Device | None:
        return self.devices.get(device_id)

    @property
    def enabled(self) -> list:
        return [d for d in self.devices.values() if d.enabled]

    @property
    def agent_urls(self) -> list:
        seen = {}
        for device in self.devices.values():
            seen.setdefault(device.agent_url, None)
        return list(seen)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# These become argv elements on the bench host. There is no shell anywhere in
# the path, so metacharacters are harmless -- but a newline or NUL would
# corrupt the argv itself, and `#` is silently truncated by the fallback YAML
# parser, which would turn a COM port into a different COM port.
_FORBIDDEN = {"\r": "carriage return", "\n": "newline", "\x00": "NUL",
              "#": "'#' (truncated by the fallback YAML parser)"}

_ARGV_FIELDS = ("target", "com_port", "tac_port", "adb_serial")


def _validate_value(problems, device_id, field_name, value):
    if value is None:
        return
    text = str(value)
    for char, label in _FORBIDDEN.items():
        if char in text:
            problems.append(
                f"{device_id}.{field_name} contains {label}: {text!r}"
            )


def _validate_schedule(problems, device_id, schedule):
    try:
        from croniter import croniter
    except ImportError:
        return  # validated for real wherever croniter is installed
    if not croniter.is_valid(schedule):
        problems.append(
            f"{device_id}.schedule is not a valid cron expression: {schedule!r}"
        )


def _validate_device(problems, device_id: str, raw: dict, defaults: dict) -> Device | None:
    if not DEVICE_ID_RE.match(device_id):
        problems.append(
            f"device id {device_id!r} must match {DEVICE_ID_RE.pattern}"
        )
        return None

    merged = {key: defaults[key] for key in INHERITABLE if key in defaults}
    merged.update({k: v for k, v in (raw or {}).items() if v is not None})

    target = merged.get("target")
    if not target:
        problems.append(f"{device_id}: 'target' is required")
    agent_url = merged.get("agent_url")
    if not agent_url:
        problems.append(f"{device_id}: 'agent_url' is required (set it under defaults)")

    if agent_url:
        parsed = urllib.parse.urlparse(str(agent_url))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            problems.append(
                f"{device_id}.agent_url must be http(s)://host[:port], got {agent_url!r}"
            )

    for field_name in _ARGV_FIELDS:
        _validate_value(problems, device_id, field_name, merged.get(field_name))

    if not target or not agent_url:
        return None

    schedule = str(merged.get("schedule", "0 1 * * *"))
    _validate_schedule(problems, device_id, schedule)

    def as_int(name, default):
        try:
            return int(merged.get(name, default))
        except (TypeError, ValueError):
            problems.append(f"{device_id}.{name} must be an integer, got {merged.get(name)!r}")
            return default

    num_boots = as_int("num_boots", 3)
    if num_boots < 1:
        problems.append(f"{device_id}.num_boots must be >= 1, got {num_boots}")
        num_boots = 1

    def as_bool(name, default):
        value = merged.get(name, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "on", "1")

    return Device(
        device_id=device_id,
        target=str(target),
        agent_url=str(agent_url).rstrip("/"),
        enabled=as_bool("enabled", True),
        com_port=_opt(merged.get("com_port")),
        tac_port=_opt(merged.get("tac_port")),
        adb_serial=_opt(merged.get("adb_serial")),
        num_boots=num_boots,
        boot_timeout=as_int("boot_timeout", 480),
        schedule=schedule,
        stages=str(merged.get("stages", "all")),
        fetch_full_logs=as_bool("fetch_full_logs", False),
        max_retries=as_int("max_retries", 1),
        notes=str(merged.get("notes", "") or ""),
    )


def _opt(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validate_shared_agents(problems, devices: list) -> None:
    by_agent: dict = {}
    for device in devices:
        by_agent.setdefault(device.agent_url, []).append(device)

    for agent_url, group in by_agent.items():
        if len(group) < 2:
            continue
        # Rule 4. This is not cosmetic: bootbench's find_console_port() probes
        # every COM port on the host and would open the other device's live
        # console mid-capture; the TAC call raises on an ambiguous device
        # count; and unqualified adb picks a board arbitrarily.
        for device in group:
            if not device.is_fully_pinned:
                missing = [
                    name for name in ("com_port", "tac_port", "adb_serial")
                    if not getattr(device, name)
                ]
                problems.append(
                    f"{device.device_id} shares agent_url {agent_url} with "
                    f"{len(group) - 1} other enabled device(s), so it must pin "
                    f"{', '.join(missing)}"
                )

        # Rule 5. Two devices with the same slug on one agent would write to
        # the same bootchart-data-<slug>.json and corrupt each other's history.
        by_slug: dict = {}
        for device in group:
            by_slug.setdefault(device.slug, []).append(device.device_id)
        for slug, ids in by_slug.items():
            if len(ids) > 1:
                problems.append(
                    f"devices {', '.join(sorted(ids))} share agent_url "
                    f"{agent_url} and target slug {slug!r}, so they would "
                    f"overwrite one another's bootchart-data-{slug}.json"
                )


def load_inventory(path) -> Inventory:
    path = Path(path)
    if not path.is_file():
        raise InventoryError([f"inventory not found: {path}"])

    try:
        raw = load_yaml(path)
    except Exception as e:
        raise InventoryError([f"could not parse {path}: {e}"])

    if not isinstance(raw, dict):
        raise InventoryError([f"{path} must contain a mapping at the top level"])

    defaults = raw.get("defaults") or {}
    device_block = raw.get("devices") or {}
    if not isinstance(device_block, dict):
        raise InventoryError([
            "'devices' must be a mapping keyed by device id, not a list -- "
            "the stdlib fallback YAML parser cannot read a list of dicts"
        ])
    if not device_block:
        raise InventoryError(["'devices' must contain at least one device"])

    problems: list = []
    devices: dict = {}
    for device_id, entry in device_block.items():
        if entry is not None and not isinstance(entry, dict):
            problems.append(f"{device_id}: expected a mapping, got {type(entry).__name__}")
            continue
        device = _validate_device(problems, str(device_id), entry or {}, defaults)
        if device is not None:
            devices[device.device_id] = device

    _validate_shared_agents(problems, [d for d in devices.values() if d.enabled])

    if problems:
        raise InventoryError(problems)

    return Inventory(devices=devices, source=path)
