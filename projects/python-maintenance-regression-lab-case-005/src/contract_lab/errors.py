"""Failures the maintenance client is allowed to raise."""

from __future__ import annotations


class ContractError(Exception):
    """Base for checked failures in this lab."""


class VersionParseError(ContractError):
    """The token is outside the study's version grammar."""

    def __init__(self, value: str):
        self.value = value
        super().__init__(f"version is outside the study grammar: {value!r}")


class HeaderGrammarError(ContractError):
    """A header was present and does not match its own grammar."""

    def __init__(self, header: str, value: str):
        self.header = header
        self.value = value
        super().__init__(f"{header} value does not match the {header} grammar")


class SchemaRejected(ContractError):
    """The active reader rejected the response body."""

    def __init__(self, node: dict, reader_results: dict, events: list):
        self.node = node
        self.reader_results = reader_results
        self.events = events
        location = node.get("instanceLocation", "")
        super().__init__(location or "schema rejected")


class UnmanagedRemoval(ContractError):
    """An element disappeared without an earlier deprecated mark."""

    def __init__(self, removals: list):
        self.removals = removals
        super().__init__("unmanaged removal")


class OperationRemoved(ContractError):
    """The operation was removed after a deprecation mark."""

    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(operation)


class OperationNotFound(ContractError):
    """The active document has no such operation and history does not explain it."""

    def __init__(self, operation: str):
        self.operation = operation
        super().__init__(operation)


class SunsetTimingError(ContractError):
    """A sunset notice was present without an observation instant."""
