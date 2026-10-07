"""Import the skill's pure functions on a machine with no bench hardware.

`bootbench.py` is a Windows program -- it drives a TAC board over COM, shells
`PCAT.exe`, and calls `powershell.exe`. None of that happens at import time:
the module body defines only constants, `main()` is `__main__`-guarded, and
`serial`/`comtypes` are imported inside the functions that need them. So the
module imports cleanly on Linux, and the coordinator can reuse its parsers
instead of growing a second implementation of the same regexes.

That matters more than it sounds. The alternative is parsing boot times in
two codebases that must agree forever, where a divergence shows up as a
number on a dashboard that is wrong by a plausible-looking amount. Here
there is exactly one implementation of "what does '12.481 s' mean".

WHAT IS SAFE TO CALL
    parse_seconds            text -> float
    build_run                parsed log files -> a run dict
    read_pulled_logs         read one boot's directory
    run_metrics_seconds      numeric view of a run dict (handles old records)
    parse_build_number       folder name -> int|None
    _build_folder_name       build path -> folder name
    _slugify                 target -> the report's filename slug
    _pick_displayed          which boots the skill's report shows
    METRIC_ROWS              the six report metrics, in display order
    EXIT_*                   the exit-code taxonomy

WHAT IS NOT
    Anything that touches a device, a share, or a report file:
    `cmd_flash`, `collect_boot_on_device`, `open_console`, `run_pcat_flash`,
    `discover_latest_build`, `save_data`, `render`, `pull_and_record`. On
    Linux they either raise or -- worse -- succeed against the wrong paths:
    `bb.BOOT_CHARTS_DIR` resolves to the skill's own directory on *this*
    host, which is not where any artifact lives. The coordinator receives
    artifacts from the agent; it never produces them.

`assert_pure()` enforces that list at runtime, so the rule is a check rather
than a comment someone reads after the fact.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

# Checked in order. The first is this repo's own layout: `../client/` holds
# the very copy deployed to the bench host, so the coordinator parses with
# the same vintage of the skill that produced the logs. The rest cover a
# checkout where the skill still lives in its own bundle.
CANDIDATES = (
    "../client/bootbench.py",
    "client/bootbench.py",
    "../../boot-skills/skills/bootbench/scripts/bootbench.py",
    "../boot-skills/skills/bootbench/scripts/bootbench.py",
)

ENV_VAR = "BOOTBENCH_SKILL_PATH"

# Names that must never be reached through this module. Every entry is
# asserted to exist in bootbench.py by tests/test_ingest.py -- a list of
# typos forbids nothing.
FORBIDDEN = frozenset({
    "main", "_run", "cmd_flash", "cmd_capture", "cmd_report",
    "cmd_latest_build",
    "pull_and_record", "capture_and_record", "collect_boot_on_device",
    "open_console", "reboot_and_relogin", "run_pcat_flash",
    "wait_for_edl_device", "wait_for_adb_device", "adb_pull_logs",
    "discover_latest_build", "find_latest_build", "find_latest_build_name",
    "resolve_build_dir", "save_data", "save_run_backup", "render",
    "render_html", "add_run", "load_data", "ensure_debug_cmdline_params",
    "remove_debug_cmdline_params",
})

_module = None
_source = None


def candidate_paths(configured=None):
    """Every path that will be tried, in order, as absolute paths."""
    paths = []
    for value in (configured, os.environ.get(ENV_VAR)):
        if value:
            path = Path(value)
            # Accept either the script or the directory holding it, because
            # both are things a person reasonably puts in a config file.
            paths.append(path / "bootbench.py" if path.is_dir() else path)
    paths.extend(ROOT / rel for rel in CANDIDATES)
    return [p if p.is_absolute() else (ROOT / p).resolve() for p in paths]


def find_bootbench(configured=None) -> Path:
    for path in candidate_paths(configured):
        if path.is_file():
            return path.resolve()
    tried = "\n  ".join(str(p) for p in candidate_paths(configured))
    raise FileNotFoundError(
        f"bootbench.py not found. Set {ENV_VAR} or `bootbench_path` in "
        f"config/server.yaml. Tried:\n  {tried}")


def load(configured=None, force=False):
    """Import bootbench.py and return the module.

    Loaded by file path rather than by package name: the skill is not
    installed, is not on sys.path, and lives at a location that differs
    between the packaged layout and a development checkout. Cached, because
    the module reads a few files at import time and ingestion calls into it
    per boot.
    """
    global _module, _source
    if _module is not None and not force:
        return _module

    path = find_bootbench(configured)
    spec = importlib.util.spec_from_file_location("bootbench_skill", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load a module from {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so a `from bootbench_skill import ...` inside
    # the module -- there is none today -- would not re-execute it.
    sys.modules["bootbench_skill"] = module
    spec.loader.exec_module(module)

    _module, _source = module, path
    log.debug("loaded bootbench from %s", path)
    return module


def source() -> Path:
    """Where the loaded module came from. For `validate-config` to print:
    silently parsing with a different vintage of the skill than the bench
    host runs is a hard problem to notice from the numbers alone."""
    if _source is None:
        load()
    return _source


def assert_pure(name: str) -> None:
    if name in FORBIDDEN:
        raise RuntimeError(
            f"bootbench.{name} touches a device, a share, or a report file "
            f"and must not be called from the coordinator -- see the module "
            f"docstring in {__name__}")


def get(name: str):
    """One checked accessor, so the FORBIDDEN list cannot be bypassed by
    simply reaching through `load()`."""
    assert_pure(name)
    module = load()
    try:
        return getattr(module, name)
    except AttributeError as e:
        raise AttributeError(
            f"bootbench.py has no '{name}' -- the skill and the coordinator "
            f"are out of step ({source()})") from e


def parse_seconds(value):
    """'12.481 s' -> 12.481, and None for anything unparseable.

    The skill's own implementation, reached through here so there is one
    definition of the rule. It is the fallback path for history recorded
    before `build_run` emitted numbers.
    """
    return get("parse_seconds")(value)


def run_metrics_seconds(run: dict) -> dict:
    return get("run_metrics_seconds")(run)


def pick_displayed(boots: list) -> list:
    return get("_pick_displayed")(boots)


def build_folder_name(build_path):
    return get("_build_folder_name")(build_path)


def parse_build_number(build_path):
    return get("parse_build_number")(build_path)


def slugify(target: str) -> str:
    return get("_slugify")(target)


def metric_keys() -> tuple:
    return tuple(row[0] for row in get("METRIC_ROWS"))


def exit_codes() -> dict:
    """{name: code} for every EXIT_* constant.

    Imported rather than restated: the coordinator's retry policy branches on
    these numbers, and a taxonomy duplicated across two codebases drifts the
    first time someone inserts a code in the middle.
    """
    module = load()
    return {name: value for name, value in vars(module).items()
            if name.startswith("EXIT_") and isinstance(value, int)}


def status_schema_version() -> int:
    return load().STATUS_SCHEMA_VERSION
