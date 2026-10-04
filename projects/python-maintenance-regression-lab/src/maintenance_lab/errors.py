"""Typed failures used by the maintenance pipeline."""

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


class DecodeError(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("decode_error", message)


class ParseError(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("parse_error", message)


class ValidationError(LabError):
    """Row-level permanent validation failure. The pipeline rejects the row."""

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        bug_guards: tuple[str, ...] = (),
    ) -> None:
        super().__init__("validation_error", message)
        self.field = field
        self.bug_guards = tuple(bug_guards)


class CheckpointError(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("checkpoint_error", message)


class StateError(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("state_error", message)


class SimulatedCrash(LabError):
    def __init__(self, message: str) -> None:
        super().__init__("simulated_crash", message)
