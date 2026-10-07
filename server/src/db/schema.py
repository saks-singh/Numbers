"""Schema paths, the canonical column lists, and DDL introspection.

The column lists here are the single definition used to build every INSERT,
so a metric is spelled once rather than once per query. They are *asserted*
against two independent sources by tests/test_schema_consistency.py:

  1. schema.sql, so a column that does not exist cannot be written to.
  2. bootbench.py's own build_run() output, so adding a metric to the skill
     fails a test here instead of being silently dropped on ingest.

That second check is the valuable one. The skill and this database are
separate codebases that only meet through a JSON document, and a new metric
appearing in the status doc with nowhere to land would otherwise go
unnoticed for as long as nobody looked at a chart.

Reading the DDL rather than asking a live server is deliberate: it means
the consistency checks run on a laptop with no Postgres installed, which is
where they are most likely to catch a typo -- before deployment.
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_SQL = HERE / "schema.sql"
MIGRATIONS_DIR = HERE / "migrations"

# The six metrics the skill's report table shows, in display order.
METRIC_KEYS = (
    "nhlos", "kernel", "initramfs",
    "sysinit_svc", "total_sysinit", "total_multiuser",
)

# The sub-components that used to exist only inside prose note strings.
SUBCOMPONENT_KEYS = (
    "grand_total", "firmware", "loader", "userspace", "sat_total",
    "init_exec", "epoch_advanced", "systemd_running", "sysinit_target",
    "cc_multiuser",
)

# Every key of a boot's `seconds` object, which is also every duration
# column on `boot` once suffixed. 16 of them.
SECONDS_KEYS = METRIC_KEYS + SUBCOMPONENT_KEYS


def seconds_column(key: str) -> str:
    """'total_multiuser' -> 'total_multiuser_s'.

    The suffix is not decoration: in a SQL console or a CSV export,
    `total_multiuser` could plausibly be milliseconds, a count, or a
    percentage, and someone will eventually assume wrong.
    """
    return f"{key}_s"


SECONDS_COLUMNS = tuple(seconds_column(k) for k in SECONDS_KEYS)

# Identity and provenance columns on `boot`, excluding the generated key.
BOOT_IDENTITY_COLUMNS = (
    "run_id", "log_name", "phase", "boot_index", "seq",
    "displayed", "recorded", "parse_error", "bootbench_ts",
)

BOOT_JSON_COLUMNS = (
    "hitters", "overall_overheads", "critical_chain", "metric_notes",
)

BOOT_CMDLINE_COLUMNS = ("kernel_cmdline", "cmdline_has_debug")

# Full insertable column list for `boot`, in a fixed order.
BOOT_COLUMNS = (
    BOOT_IDENTITY_COLUMNS + SECONDS_COLUMNS
    + BOOT_JSON_COLUMNS + BOOT_CMDLINE_COLUMNS
)

# Columns ingestion writes on `run` from the status document. Queue-side
# columns (status, queued_at, trigger_source, ...) are set at enqueue time
# and are deliberately absent -- ingest must not reset them.
RUN_INGEST_COLUMNS = (
    "target", "host", "build_number", "build_folder", "build_path",
    "share_root", "exit_code", "failure_stage", "error_class",
    "error_message", "stages_requested", "stages_run", "reflashed",
    "boots_expected", "boots_recorded", "started_at", "finished_at",
    "status_json", "schema_version", "agent_data_json", "agent_report_html",
    "agent_boot_logs_dir",
)

# Columns the device projection upserts from devices.yaml.
DEVICE_COLUMNS = (
    "device_id", "target", "slug", "agent_url", "enabled",
    "com_port", "tac_port", "adb_serial", "num_boots", "boot_timeout",
    "schedule", "stages", "fetch_full_logs", "max_retries", "notes",
)

# ---------------------------------------------------------------------------
# DDL introspection
# ---------------------------------------------------------------------------

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\);",
    re.S | re.I,
)
_CREATE_VIEW_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)\s+AS", re.I)
_VIEW_BODY_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)\s+AS\s+SELECT\b(.*?);",
    re.S | re.I)
_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)", re.I)
_CREATE_TYPE_RE = re.compile(r"CREATE\s+TYPE\s+(\w+)\s+AS\s+ENUM", re.I)

# A line that starts a column definition, as opposed to a table constraint
# or a comment. Table-level constraints are not columns and must not be
# reported as such.
_CONSTRAINT_WORDS = ("unique", "primary", "foreign", "check", "constraint",
                     "exclude")


def _strip_comments(sql: str) -> str:
    return "\n".join(
        line.split("--", 1)[0] if "--" in line else line
        for line in sql.splitlines()
    )


def _split_top_level(text: str) -> list:
    """Split on commas that are not inside brackets.

    `NUMERIC(10,3)` and `DISTINCT ON (a, b)` both contain commas that do not
    separate items, so a plain `text.split(",")` mis-parses the schema.
    """
    parts, depth, current = [], 0, ""
    for char in text:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def parse_tables(sql=None) -> dict:
    """{table_name: [column_name, ...]} parsed from the DDL.

    A deliberately small parser: it reads the one dialect this file is
    written in, not SQL in general. If it ever stops understanding
    schema.sql the consistency tests fail loudly rather than quietly
    checking nothing -- test_schema_consistency asserts the parse found
    every table it expects.
    """
    sql = sql if sql is not None else SCHEMA_SQL.read_text(encoding="utf-8")
    body = _strip_comments(sql)

    tables = {}
    for name, block in _CREATE_TABLE_RE.findall(body):
        parsed = []
        for fragment in _split_top_level(block):
            fragment = fragment.strip()
            if not fragment:
                continue
            first = fragment.split()[0]
            if first.lower() in _CONSTRAINT_WORDS:
                continue
            parsed.append(first)
        tables[name] = parsed
    return tables


def parse_view_columns(sql=None) -> dict:
    """{view_name: [output_column, ...]}, or ['*'] for a SELECT *.

    Exists because `boot_trend` is a hand-written column list and the chart
    query builds its own from SECONDS_COLUMNS. A metric added to the skill
    but not to the view would otherwise fail at runtime with
    `UndefinedColumn` on a page nobody loads until a regression appears.
    """
    sql = sql if sql is not None else SCHEMA_SQL.read_text(encoding="utf-8")
    body = _strip_comments(sql)

    views = {}
    for match in _VIEW_BODY_RE.finditer(body):
        name, select = match.group(1), match.group(2)
        # Cut at the FROM that closes the select list, not one inside a
        # subquery or a function call.
        depth = 0
        end = len(select)
        for token in re.finditer(r"[()]|\bFROM\b", select, re.I):
            text = token.group(0)
            if text == "(":
                depth += 1
            elif text == ")":
                depth -= 1
            elif depth == 0:
                end = token.start()
                break
        columns = []
        for item in _split_top_level(select[:end]):
            item = item.strip().rstrip(";")
            if not item:
                continue
            item = re.sub(r"^DISTINCT\s+ON\s*\([^)]*\)\s*", "", item, flags=re.I)
            item = re.sub(r"^DISTINCT\s+", "", item, flags=re.I)
            if not item:
                continue
            alias = re.search(r"\s+AS\s+(\w+)\s*$", item, re.I)
            if alias:
                columns.append(alias.group(1))
            elif item.strip() == "*" or item.strip().endswith(".*"):
                columns.append("*")
            else:
                columns.append(item.strip().split(".")[-1].split()[0])
        views[name] = columns
    return views


def parse_views(sql=None) -> list:
    sql = sql if sql is not None else SCHEMA_SQL.read_text(encoding="utf-8")
    return _CREATE_VIEW_RE.findall(_strip_comments(sql))


def parse_indexes(sql=None) -> dict:
    sql = sql if sql is not None else SCHEMA_SQL.read_text(encoding="utf-8")
    return {name: table
            for name, table in _CREATE_INDEX_RE.findall(_strip_comments(sql))}


def parse_enums(sql=None) -> list:
    sql = sql if sql is not None else SCHEMA_SQL.read_text(encoding="utf-8")
    return _CREATE_TYPE_RE.findall(_strip_comments(sql))


def migration_files() -> list:
    """Every migration, in name order. Zero-padded numeric prefixes make
    lexical order the intended order; a file named without one would sort
    unpredictably, so initdb rejects it."""
    if not MIGRATIONS_DIR.is_dir():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
