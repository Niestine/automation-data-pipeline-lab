"""Typed failures used by the runner, stores, and handlers."""

from __future__ import annotations

from typing import Optional


class LabError(Exception):
    """Base class for lab errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SchemaError(LabError):
    def __init__(self, message: str, errors: Optional[list[str]] = None) -> None:
        super().__init__("schema_error", message)
        self.errors = list(errors or [])


class RetryableError(LabError):
    """Transient handler/runtime failures that may be retried."""


class PermanentError(LabError):
    """Failures that must not be retried."""


class JobTimeout(RetryableError):
    def __init__(self, message: str = "handler timed out") -> None:
        super().__init__("timeout", message)


class TransientError(RetryableError):
    def __init__(self, message: str = "transient I/O") -> None:
        super().__init__("transient_io", message)


class ValidationError(PermanentError):
    def __init__(self, message: str) -> None:
        super().__init__("validation_error", message)


class IdempotencyConflict(PermanentError):
    def __init__(self, message: str) -> None:
        super().__init__("idempotency_conflict", message)


class CheckpointError(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("checkpoint_error", message)


class StateError(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("state_error", message)


class SimulatedCrash(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("simulated_crash", message)
