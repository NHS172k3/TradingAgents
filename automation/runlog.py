"""Logging setup for the automation layer.

Provides a single entry point (get_logger) that configures stderr and dated
file logging. Handlers are attached to a parent logger to avoid duplication.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sys

from automation import settings

# Single parent logger for all automation child loggers
_PARENT_LOGGER_NAME = "automation"
_INITIALIZED = False


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of the 'automation' parent.

    Idempotent: calling twice with the same or different names does not
    duplicate handlers. Logs to both stderr and a dated file in logs/.

    Args:
        name: Logger name, typically __name__.

    Returns:
        A logger instance as a child of the 'automation' parent.
    """
    global _INITIALIZED

    parent = logging.getLogger(_PARENT_LOGGER_NAME)

    if not _INITIALIZED:
        # Ensure logs directory exists
        settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)

        # Format: timestamp levelname logger_name: message
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

        # Stderr handler
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(formatter)
        stderr_handler.setLevel(logging.INFO)
        parent.addHandler(stderr_handler)

        # Dated file handler (logs/run_YYYY-MM-DD.log)
        today = _dt.date.today().isoformat()
        log_file = settings.LOGS_DIR / f"run_{today}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        parent.addHandler(file_handler)

        parent.setLevel(logging.INFO)
        _INITIALIZED = True

    if name == _PARENT_LOGGER_NAME or name.startswith(f"{_PARENT_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_PARENT_LOGGER_NAME}.{name}")
