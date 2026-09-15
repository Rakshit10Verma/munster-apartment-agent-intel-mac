from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(root: Path, level: str = "INFO") -> None:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    app_logger = logging.getLogger("apartment_agent")
    app_logger.setLevel(getattr(logging, level, logging.INFO))
    if not app_logger.handlers:
        handler = RotatingFileHandler(
            log_dir / "agent.jsonl", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(JsonFormatter())
        app_logger.addHandler(handler)
    app_logger.propagate = False
