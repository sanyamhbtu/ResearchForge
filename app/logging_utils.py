"""Structured (JSON-lines) logging plus an in-memory ring buffer.

The API exposes the recent log buffer so the UI can show real operational
events (requests, retries, rate limits, failures) instead of decorative
progress bars.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import deque
from typing import Any

_RING: deque[dict] = deque(maxlen=600)
_RING_LOCK = threading.Lock()


def _record_to_dict(record: logging.LogRecord) -> dict:
    payload: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
        "level": record.levelname,
        "logger": record.name,
        "event": record.getMessage(),
    }
    for key, value in getattr(record, "extra_fields", {}).items():
        payload[key] = value
    if record.exc_info:
        payload["error"] = logging.Formatter().formatException(record.exc_info)
    return payload


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = _record_to_dict(record)
        except Exception:  # pragma: no cover - logging must never raise
            return
        with _RING_LOCK:
            _RING.append(payload)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_record_to_dict(record), ensure_ascii=False)


class StructuredLogger:
    """Small wrapper so call sites can pass structured fields."""

    __slots__ = ("_logger",)

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, event: str, **fields: Any) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, event, extra={"extra_fields": fields})

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._logger.error(event, exc_info=True, extra={"extra_fields": fields})


_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger("researchforge")
    root.setLevel(level)
    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(_JsonFormatter())
    root.addHandler(stream)
    root.addHandler(_RingHandler())
    root.propagate = False
    # uvicorn access logs are noisy and duplicate our own request logging.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(f"researchforge.{name}")


def recent_logs(limit: int = 200, min_level: str = "INFO") -> list[dict]:
    order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    threshold = order.get(min_level.upper(), 20)
    with _RING_LOCK:
        items = list(_RING)
    filtered = [item for item in items if order.get(item.get("level", "INFO"), 20) >= threshold]
    return filtered[-limit:]
