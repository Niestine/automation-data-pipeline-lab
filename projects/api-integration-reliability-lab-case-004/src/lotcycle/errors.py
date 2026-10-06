"""Exceptions the cold-lot grant lab raises on purpose."""

from __future__ import annotations


class LabError(Exception):
    """Base error for expected lab failures."""


class ReauthRequired(LabError):
    """The refresh family can no longer be used. The client must stop."""


class RefreshBudgetExhausted(LabError):
    """Pre-commit refresh attempts ran out. The family is still active."""


class TokenEndpointError(LabError):
    """A classified token-endpoint error. Terminal for that attempt."""

    def __init__(self, error: str, status: int):
        super().__init__(error)
        self.error = error
        self.status = status


class ResponseDropped(LabError):
    """The request bytes were written and the response never arrived."""


class ConnectionClosed(LabError):
    """The request bytes were not written."""


class CrashBeforeCommit(LabError):
    """Archive bytes were received and the checkpoint transaction rolled back."""


class SchemaError(LabError):
    """A request body failed local schema checks."""
