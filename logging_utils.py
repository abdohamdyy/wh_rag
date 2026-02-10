import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import UUID

from chat_db import insert_event


def _ensure_logs_dir() -> Path:
    logs_dir = Path(os.getenv("LOG_DIR", "logs"))
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def setup_logging() -> logging.Logger:
    """
    Sets up a single rotating file logger.
    Call once on app startup.
    """
    logger = logging.getLogger("app")
    if getattr(logger, "_configured", False):
        return logger

    level_name = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    logs_dir = _ensure_logs_dir()
    log_path = logs_dir / "app.log"

    handler = RotatingFileHandler(
        log_path,
        maxBytes=int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024))),  # 5MB
        backupCount=int(os.getenv("LOG_BACKUP_COUNT", "5")),
        encoding="utf-8",
    )
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    # Also log to console (uvicorn captures stdout/stderr)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    logger._configured = True  # type: ignore[attr-defined]
    return logger


def _truncate(s: Any, max_len: int = 2000) -> Any:
    if s is None:
        return None
    if isinstance(s, (int, float, bool)):
        return s
    if isinstance(s, dict):
        return {k: _truncate(v, max_len=max_len) for k, v in s.items()}
    if isinstance(s, list):
        return [_truncate(x, max_len=max_len) for x in s[:200]]
    st = str(s)
    if len(st) <= max_len:
        return st
    return st[: max_len - 1] + "…"


def log_event(
    logger: logging.Logger,
    *,
    correlation_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    conversation_id: int | None = None,
    also_store_in_db: bool = True,
) -> None:
    safe_payload = _truncate(payload or {})
    msg = json.dumps(
        {"correlation_id": str(correlation_id), "event_type": event_type, "payload": safe_payload},
        ensure_ascii=False,
        default=str,
    )
    logger.info(msg)

    if also_store_in_db:
        try:
            insert_event(
                correlation_id=correlation_id,
                event_type=event_type,
                payload=safe_payload,
                conversation_id=conversation_id,
            )
        except Exception as e:
            logger.error(
                json.dumps(
                    {
                        "correlation_id": str(correlation_id),
                        "event_type": "log_event_db_failed",
                        "error": str(e),
                    },
                    ensure_ascii=False,
                )
            )


