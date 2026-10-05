"""Expected failures for the shift-slip lease queue."""

from __future__ import annotations


class ShiftError(Exception):
    def __init__(self, code: str, message: str, http_class: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_class = http_class


class SubmitError(ShiftError):
    def __init__(
        self,
        code: str,
        message: str,
        http_class: int,
        job_id: int | None = None,
    ) -> None:
        super().__init__(code, message, http_class)
        self.job_id = job_id


class ValidationError(ShiftError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_payload", message, 422)


class BusyError(ShiftError):
    def __init__(self) -> None:
        super().__init__("sqlite_busy", "database is busy", 503)


class OpenError(ShiftError):
    def __init__(self, message: str) -> None:
        super().__init__("open_failed", message, 500)


class CrashInjected(Exception):
    """Test fault. A production worker does not raise this."""

    def __init__(self, point: str) -> None:
        super().__init__(point)
        self.point = point


class EffectConflict(Exception):
    """The outbox already has different bytes for this idempotency key."""


class EffectExhausted(Exception):
    """Effect retries used their own cap. The claim attempt is left to expire."""
