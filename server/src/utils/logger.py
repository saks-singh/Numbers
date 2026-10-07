"""Logging setup for the coordinator's three long-lived entry points.

Simpler than aibench's PhaseLogger on purpose: there are no per-run
lifecycle phases here. `tick`, `worker`, and `serve` each want one stream
that goes to stdout (so systemd/cron capture it) and optionally to a
rotating file.

Timestamps are UTC with an explicit `Z`. The bench hosts are in whatever
zone they are in and the build share publishes on its own schedule; a log
line that does not say which zone it means is a log line you cannot
correlate.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%SZ"

_configured = False


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime


def setup_logging(level="INFO", log_file=None, max_bytes=20 * 1024 * 1024,
                  backups=5) -> logging.Logger:
    """Configure the `bootbench` logger tree. Idempotent -- calling it twice
    does not double every line, which matters because `serve` imports modules
    that may each want a logger."""
    global _configured

    root = logging.getLogger("bootbench")
    if _configured:
        return root

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # Our own handlers only. Flask and waitress configure the real root
    # logger; propagating would print every line twice under `serve`.
    root.propagate = False
    root.handlers.clear()

    formatter = _UTCFormatter(fmt=FORMAT, datefmt=DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)

    _configured = True
    return root


def get_logger(name: str = "") -> logging.Logger:
    """`get_logger("worker")` -> the `bootbench.worker` logger."""
    return logging.getLogger(f"bootbench.{name}" if name else "bootbench")
