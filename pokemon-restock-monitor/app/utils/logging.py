"""Structured logging.

Use ``log_event(logger, "inventory_check", retailer=..., sku=...)``. The
fields are rendered as ``key=value`` pairs in console format, or as a JSON
object when ``LOG_FORMAT=json``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_FIELDS_ATTR = "structured_fields"


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        fields = getattr(record, _FIELDS_ATTR, None) or {}
        rendered = " ".join(f"{k}={_render(v)}" for k, v in fields.items() if v is not None)
        line = f"{ts} | {record.levelname:<7} | {record.name} | {record.getMessage()}"
        if rendered:
            line += f" | {rendered}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, _FIELDS_ATTR, None) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _render(value) -> str:
    text = str(value)
    return f'"{text}"' if " " in text else text


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "apscheduler.scheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, event, extra={_FIELDS_ATTR: fields})
