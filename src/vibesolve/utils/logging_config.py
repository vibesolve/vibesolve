"""
Structured logging configuration using structlog.

Call configure_logging() once at process startup (in the CLI entry point).
Each pipeline run then gets its own BoundLogger via get_run_logger(), which
tags every log line with problem and worker context automatically.

File output is handled by a custom FileWriteProcessor injected per run, so
parallel workers each write to their own log file without sharing handlers.
"""

from pathlib import Path
from typing import Any

import structlog


def configure_logging() -> None:
    """Configure structlog for the process. Call once at startup."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=True, pad_level=False),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


class _FileProcessor:
    """Structlog processor that appends a plain-text line to a log file."""

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, logger: Any, method: str, event_dict: dict) -> dict:
        ts = event_dict.get("timestamp", "")
        event = event_dict.get("event", "")
        extras = {
            k: v
            for k, v in event_dict.items()
            if k not in ("timestamp", "level", "event")
        }
        line = f"[{ts}] {event}"
        if extras:
            line += "  " + "  ".join(f"{k}={v}" for k, v in extras.items())
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        # Return event_dict unchanged so downstream processors still run
        return event_dict


def get_run_logger(
    log_path: Path,
    **context: Any,
) -> structlog.BoundLogger:
    """
    Return a BoundLogger pre-bound with the given context keys.

    The logger writes to both stdout (via configure_logging()) and the
    specified log file (via a per-run _FileProcessor).

    Args:
        log_path: File to append plain-text log lines to.
        **context: Arbitrary key-value pairs bound to every log call,
                   e.g. problem="timetable.txt", worker="container-0".
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_proc = _FileProcessor(log_path)

    # Build a logger chain that appends to the file before rendering to stdout.
    # We wrap around structlog's chain by using a bound logger with an extra
    # processor injected via new_logger.
    return (
        structlog.wrap_logger(
            structlog.PrintLogger(),
            processors=[
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
                file_proc,
                structlog.dev.ConsoleRenderer(colors=False, pad_level=False),
            ],
            wrapper_class=structlog.BoundLogger,
            context_class=dict,
        )
        .bind(**context)
    )
