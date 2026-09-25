"""Structured logs. Sensitive headers and registered secrets are removed."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from mbet.redact import redact, redact_text

_CONFIGURED = False


class RedactingFormatter(logging.Formatter):
    """One JSON object per line. The message is scrubbed before it is emitted."""

    def format(self, record: logging.LogRecord) -> str:
        message = redact_text(record.getMessage())
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["error"] = record.exc_info[0].__name__
        for key, value in record.__dict__.items():
            if key in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "message",
                "taskName",
            }:
                continue
            if key.startswith("_"):
                continue
            payload[key] = redact(value)
        return json.dumps(payload, default=str)


class StderrHandler(logging.StreamHandler):
    """Write to the current stderr so test runners can swap the stream."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        try:
            super().emit(record)
        except ValueError:
            return


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    logger = logging.getLogger("mbet")
    logger.handlers.clear()
    logger.setLevel(level)
    handler = StderrHandler()
    handler.setLevel(level)
    handler.setFormatter(RedactingFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(f"mbet.{name}")
