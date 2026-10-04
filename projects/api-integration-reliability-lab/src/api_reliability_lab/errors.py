"""Typed failures used by the client, mock, and webhook receiver."""

from __future__ import annotations

from typing import Any, Optional


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


class TransportError(LabError):
    """Network-shaped failures raised by a transport (timeout, reset)."""


class ApiError(LabError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        retryable: bool,
        payload: Any = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(code, message)
        self.status = status
        self.retryable = retryable
        self.payload = payload
        self.request_id = request_id


class AuthError(ApiError):
    def __init__(
        self,
        message: str = "unauthorized",
        *,
        status: int = 401,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            "auth",
            message,
            status=status,
            retryable=False,
            request_id=request_id,
        )


class IdempotencyConflict(ApiError):
    def __init__(self, message: str, *, request_id: Optional[str] = None) -> None:
        super().__init__(
            "idempotency_conflict",
            message,
            status=409,
            retryable=False,
            request_id=request_id,
        )


class CheckpointError(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("checkpoint_error", message)


class SimulatedCrash(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("simulated_crash", message)
