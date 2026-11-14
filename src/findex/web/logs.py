"""Logs to stdout, one line per event, each tagged with the request id.

Platforms (Render, Fly.io, Docker) collect stdout; nothing is written to
files. ``LOG_FORMAT=json`` gives one JSON object per line for log search.
``request_id`` is a context variable set by the middleware, so a log call
anywhere during a request — in a pool thread too, the context is copied —
carries the id that the client sees in the ``X-Request-ID`` header.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

request_id: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def logging_config(level: str = "INFO", fmt: str = "text") -> dict[str, Any]:
    """A ``dictConfig`` for uvicorn's ``log_config``: applied in every worker."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"rid": {"()": RequestIdFilter}},
        "formatters": {
            "text": {
                "format": "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s"
            },
            "json": {"()": JsonFormatter},
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": fmt,
                "filters": ["rid"],
            }
        },
        "root": {"level": level, "handlers": ["stdout"]},
        "loggers": {
            "uvicorn": {"level": level, "handlers": [], "propagate": True},
            "uvicorn.error": {"level": level, "handlers": [], "propagate": True},
            "uvicorn.access": {"level": "WARNING", "handlers": [], "propagate": True},
        },
    }
