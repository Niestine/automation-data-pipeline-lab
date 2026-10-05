"""Redact emails, fixture bodies, and secret-shaped fields before persistence."""

from __future__ import annotations

import logging
import re
from typing import Any

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRET_KEYS = frozenset({"password", "secret", "token", "api_key", "authorization"})


def is_secret_key(name: str) -> bool:
    lowered = name.lower()
    if lowered in SECRET_KEYS:
        return True
    return lowered.endswith(("_password", "_secret", "_token", "_api_key"))


def redact(value: Any, bodies: set[str]) -> Any:
    ordered = sorted((body for body in bodies if body), key=len, reverse=True)
    return _redact(value, ordered)


def _redact(value: Any, bodies: list[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if is_secret_key(str(key)) else _redact(item, bodies)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, bodies) for item in value]
    if isinstance(value, str):
        text = value
        for body in bodies:
            if body in text:
                text = text.replace(body, "[REDACTED_BODY]")
        return EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    return value


class RedactFilter(logging.Filter):
    """Drop raw addresses from log lines emitted by this lab."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = EMAIL_RE.sub("[REDACTED_EMAIL]", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                EMAIL_RE.sub("[REDACTED_EMAIL]", item) if isinstance(item, str) else item
                for item in record.args
            )
        return True


def lab_logger() -> logging.Logger:
    logger = logging.getLogger("office_gate_lab")
    if not any(isinstance(item, RedactFilter) for item in logger.filters):
        logger.addFilter(RedactFilter())
    return logger
