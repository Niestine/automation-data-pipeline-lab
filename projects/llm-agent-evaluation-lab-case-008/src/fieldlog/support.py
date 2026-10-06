"""Clock, token accounting, time expressions, and shared errors."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("fieldlog")

INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class FieldlogError(Exception):
    """Base error for the memory lab."""


class RetryableError(FieldlogError):
    """Transient extractor failure. Safe to retry; nothing was committed."""


class FlushIntegrityError(FieldlogError):
    """A flush would drop a declared fact that has no stored edge."""


class PolicyError(FieldlogError):
    """A write or answer violated a closed policy."""


class RouterError(FieldlogError):
    """The question router could not emit a schema-valid type."""


class SchemaRejected(FieldlogError):
    """An instance or a schema document failed validation."""


class CitationError(FieldlogError):
    """A resolved answer cited an episode that is not in the log."""


class Journal:
    """In-memory event log. The same records are written to logging."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.tool_log: list[dict] = []

    def add(self, **payload: object) -> None:
        if "event" not in payload:
            raise FieldlogError("journal records require an event name")
        self.events.append(dict(payload))
        logger.info("%s", payload["event"])

    def tool(self, name: str, arguments: dict) -> None:
        record = {"tool": name, "arguments": dict(arguments)}
        self.tool_log.append(record)
        self.add(event="tool", tool=name)


class Clock:
    """Injectable UTC clock. sleep records backoff and does not block."""

    def __init__(self, start: str = "2026-04-01T00:00:00Z") -> None:
        self._dt = _parse_utc(start)
        self.sleeps: list[float] = []

    def now(self) -> str:
        return self._dt.strftime(INSTANT_FORMAT)

    def advance(self, seconds: int) -> None:
        self._dt += timedelta(seconds=int(seconds))

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.advance(1)


def count_tokens(text: str) -> int:
    """Whitespace word count. Budgets in this lab use this unit, not a BPE tokenizer."""

    if not text or not text.strip():
        return 0
    return len(text.split())


def parse_instant(value: str) -> str:
    """Require a real timezone-aware instant. A format annotation is not this check."""

    if not isinstance(value, str):
        raise ValueError("timestamp must be a string instant")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"not an instant: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    normalized = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.strftime(INSTANT_FORMAT)


def resolve_time_expression(expr: str | None, reference: str) -> str | None:
    """Resolve absolute and a small relative vocabulary against the episode reference time."""

    if expr is None or expr == "":
        return None
    token = expr.strip()
    ref = _parse_utc(parse_instant(reference))
    if token == "today":
        return parse_instant(reference)
    if token == "yesterday":
        return (ref - timedelta(days=1)).strftime(INSTANT_FORMAT)
    if token.endswith(" days ago"):
        days = _leading_int(token[: -len(" days ago")])
        return (ref - timedelta(days=days)).strftime(INSTANT_FORMAT)
    if token.startswith("in ") and token.endswith(" days"):
        days = _leading_int(token[3 : -len(" days")])
        return (ref + timedelta(days=days)).strftime(INSTANT_FORMAT)
    return parse_instant(token)


def call_with_retry(fn, clock: Clock, max_attempts: int = 3, base_delay: float = 0.05, journal: Journal | None = None):
    """Exponential backoff on RetryableError. The callable must not commit state."""

    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except RetryableError as exc:
            last = exc
            if attempt == max_attempts - 1:
                break
            delay = base_delay * (2 ** attempt)
            if journal is not None:
                journal.add(event="retry", attempt=attempt + 1, delay=delay)
            clock.sleep(delay)
    assert last is not None
    raise last


def _parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _leading_int(token: str) -> int:
    if not token.isdigit():
        raise ValueError(f"expected an integer day count, got {token!r}")
    return int(token)
