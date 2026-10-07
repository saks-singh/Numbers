"""YAML loading with a stdlib fallback.

Mirrors aibench/src/utils/config_loader.py: prefer PyYAML, degrade to a
small indentation parser so a missing dependency never stops the
coordinator from reading its own config.

The fallback cannot parse a list of dicts and strips anything after a `#`,
which is why devices.yaml is a map keyed by device id and why no config
value may contain `#`. validate-config enforces that rather than leaving it
as folklore.
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def _coerce(value: str):
    lowered = value.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", "~", ""):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_simple_yaml(content: str) -> dict:
    """Indentation-based parser for nested maps of scalars. No list support --
    devices.yaml deliberately avoids lists so this path stays correct."""
    result: dict = {}
    stack = [(-1, result)]

    for raw in content.splitlines():
        line = raw.split("#", 1)[0] if "#" in raw else raw
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue

        indent = len(line) - len(line.lstrip())
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if value:
            parent[key] = _coerce(value)
        else:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))

    return result


def load_yaml(path: Path) -> dict:
    content = Path(path).read_text(encoding="utf-8")
    if HAVE_YAML:
        return yaml.safe_load(content) or {}
    return parse_simple_yaml(content)
