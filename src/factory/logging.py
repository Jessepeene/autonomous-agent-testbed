"""Structured JSON logging.

A factory that nobody watches is only debuggable through its logs, so every record
is machine-parseable and carries the engine/task context that produced it.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

#: One log file's ceiling before it rotates, and how many rotations are kept --
#: ~200MB total worst case, against a host that has run as low as 16GB free.
_MAX_LOG_BYTES = 50 * 1024 * 1024
_LOG_BACKUPS = 3


def configure_logging(
    level: str = "INFO", *, pretty: bool = False, file_path: str | None = None
) -> None:
    """Configure structured logging, optionally also to a durable file.

    stdout alone is not a usable record of what this factory did. Docker's
    json-file driver stores a container's logs under that container's id, and
    `docker compose up -d` *replaces* the container on every deploy -- so each
    deploy silently destroys the history of everything that happened before it.
    Diagnosing a real incident meant reconstructing it from database inference
    because the log lines describing it no longer existed.

    `file_path` writes the same JSON records to a bind-mounted host path that
    outlives any container, rotated so an unattended factory cannot fill the disk.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if file_path:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        try:
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    file_path, maxBytes=_MAX_LOG_BYTES, backupCount=_LOG_BACKUPS
                )
            )
        except OSError as exc:
            # A misconfigured or unwritable path must never stop the factory
            # booting -- stdout logging still works, and the failure says so.
            print(f'{{"event": "logging.file_unavailable", "detail": "{exc}"}}', file=sys.stdout)

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    # httpx logs every request at INFO with its full URL, and Telegram's API puts
    # the bot token *in the URL path* (`/bot<token>/getUpdates`). On stdout that was
    # merely untidy; written to a durable rotated file it is a live credential
    # persisted to disk. Silenced to WARNING here rather than filtered downstream,
    # because the safe default is not to emit it at all. Transport failures still
    # surface: every caller in this codebase logs its own error with the detail that
    # actually matters, and httpx's own warnings and above still pass.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    renderer: Any = (
        structlog.dev.ConsoleRenderer() if pretty else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        # Route through stdlib logging rather than structlog's default
        # PrintLoggerFactory, which writes straight to sys.stdout and bypasses
        # every handler configured above -- so the durable file received uvicorn's
        # lines and not one of this factory's own events, which are the entire
        # point of keeping it.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
