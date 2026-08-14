"""Logging: stdout/stderr by default, optional file, optional JSON.

Requiring a log directory to exist before the robot can run would break the
"just run it from a shell" contract, and would also fight with containers and
systemd, which capture the standard streams themselves. So the default sink is
the standard streams and the file is purely additive.

Records are split by severity: WARNING and below go to stdout, ERROR and above
to stderr, so a wrapper script can treat stderr as the thing worth paging on.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import LoggingConfig

# Attributes present on every LogRecord; anything else was attached by us via
# `extra=` and belongs in the structured output.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None))
) | {"asctime", "message", "taskName"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _TextFormatter(logging.Formatter):
    """Human-readable, with any structured context appended as key=value pairs."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and not key.startswith("_")
        }
        if extras:
            rendered = " ".join(f"{key}={value!r}" for key, value in sorted(extras.items()))
            base = f"{base} [{rendered}]"
        return base


class _MaxLevelFilter(logging.Filter):
    def __init__(self, maximum: int) -> None:
        super().__init__()
        self.maximum = maximum

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.maximum


def configure(config: LoggingConfig) -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    formatter: logging.Formatter = (
        _JsonFormatter() if config.format == "json" else _TextFormatter()
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(getattr(logging, config.level.upper(), logging.INFO))

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)
    stdout.addFilter(_MaxLevelFilter(logging.WARNING))
    root.addHandler(stdout)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    stderr.setLevel(logging.ERROR)
    root.addHandler(stderr)

    if config.file is not None:
        path = Path(config.file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # httpx narrates every request at INFO, which buries our own output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
