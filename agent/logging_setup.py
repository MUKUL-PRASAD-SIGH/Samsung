"""Structured logging (Phase G): JSON lines carrying the session, epoch and request id of whatever was being handled.

Context lives in `contextvars`, so it follows the asyncio task that handles a session (and the tasks it spawns) without
every log call having to pass it. `LOG_FORMAT=json` for log shippers, `text` (default) for humans.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any, Dict, Optional

request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)
session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("session_id", default=None)
epoch_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("epoch", default=None)

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.session_id = session_id_var.get()
        record.epoch = epoch_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "session_id", "epoch"):
            value = getattr(record, key, None)
            if value is not None:
                out[key] = value
        for key, value in record.__dict__.items():          # anything passed via extra={...}
            if key not in _STANDARD and key not in out and not key.startswith("_") and key not in ("request_id", "session_id", "epoch"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(name)s%(ctx)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        parts = [f"{k}={v}" for k, v in (("sid", getattr(record, "session_id", None)), ("epoch", getattr(record, "epoch", None)),
                                         ("req", getattr(record, "request_id", None))) if v is not None]
        record.ctx = f" [{' '.join(parts)}]" if parts else ""
        return super().format(record)


def setup_logging(fmt: str = "text", level: str = "INFO") -> None:
    """Idempotent: replaces the root handler so calling it twice (reload, tests) never duplicates lines."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_agent_handler", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler._agent_handler = True  # type: ignore[attr-defined]
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
