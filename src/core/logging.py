"""
Logging bootstrap.

Call `configure_logging(settings)` once at startup. Every log record will then
carry whatever `request_id`, `path`, `method`, `status`, `duration_ms` fields
are attached via `logger.info(..., extra={...})`.

Two formatters:
  * `json`   — production. One JSON object per line. Easy for log shippers / Loki.
  * `pretty` — local development. Human-readable timestamps + colored level.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.config import Settings


# Fields we promote from `record.__dict__` into the structured payload.
_STRUCTURED_EXTRAS = ("request_id", "path", "method", "status", "duration_ms")


class JsonFormatter(logging.Formatter):
    """Serialize each LogRecord as a compact JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
        }

        # Promote known structured extras (request context) if present on the record.
        for key in _STRUCTURED_EXTRAS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]

        # Always include traceback info for exceptions so logs are debuggable.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class PrettyFormatter(logging.Formatter):
    """Single-line formatter for humans — includes extras when present."""

    _BASE_FMT = "%(asctime)s %(levelname)-5s %(name)s :: %(message)s"

    def __init__(self) -> None:
        super().__init__(fmt=self._BASE_FMT, datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        # Append structured extras as key=value pairs so they remain visible in dev.
        pairs = " ".join(
            f"{k}={record.__dict__[k]!r}"
            for k in _STRUCTURED_EXTRAS
            if k in record.__dict__
        )
        return f"{base}  {pairs}" if pairs else base


def configure_logging(settings: Settings) -> None:
    """
    Install our formatter on the root logger and set the log level.

    Safe to call multiple times (e.g. during tests or after a config reload) —
    any previously-installed handlers are cleared first so we never stack them.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    formatter: logging.Formatter = JsonFormatter() if settings.log_format.lower() == "json" else PrettyFormatter()

    # Build the single stdout handler we use.
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    # Reset the root logger so re-configuration is idempotent.
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Phase-2 observability: when WLS_LOG_FILE is set, also write to a
    # rotating file so Promtail can tail it and ship to Loki.
    file_path = os.environ.get("WLS_LOG_FILE")
    if file_path:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            file_path, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Calm down the noisy third-party loggers we don't control.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
