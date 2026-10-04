"""Typed failures used by the collector, fixture site, and CLI."""

from __future__ import annotations

from typing import Optional


class LabError(Exception):
    """Base class for lab errors."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SchemaError(LabError):
    def __init__(self, message: str, errors: Optional[list[str]] = None, *, field: str = "") -> None:
        super().__init__("schema_error", message)
        self.errors = list(errors or [])
        self.field = field


class ParseError(LabError):
    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__("parse_error", message)
        self.field = field


class TransportError(LabError):
    """Network-shaped failures raised by a transport (timeout, reset)."""


class HttpError(LabError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        retryable: bool,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(code, message)
        self.status = status
        self.retryable = retryable
        self.request_id = request_id


class RobotsDenied(LabError):
    def __init__(self, path: str) -> None:
        super().__init__("robots_denied", f"robots.txt disallows {path}")
        self.path = path


class CheckpointError(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("checkpoint_error", message)


class SimulatedCrash(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("simulated_crash", message)
