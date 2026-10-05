"""Exceptions the lab surfaces instead of swallowing."""

from __future__ import annotations


class LabError(Exception):
    """Base for expected lab failures."""


class SchemaError(LabError):
    """A payload failed validation before any write."""


class MissingIdempotencyKey(LabError):
    """POST upsert was sent without Idempotency-Key."""


class MalformedIdempotencyKey(LabError):
    """The header was not a quoted UUID string."""


class InvalidArgument(LabError):
    """The call is structurally invalid (page size, token, filter, order)."""


class TokenDenied(LabError):
    """The caller is not the principal that minted the page token."""


class CheckpointIOError(LabError):
    """The barrier record could not be written. Rows may already be committed."""


class LeaseDenied(LabError):
    """Another worker holds the scope lease."""


class CheckpointMismatch(LabError):
    """The stored parameter fingerprint does not match this run."""


class ResponseDropped(LabError):
    """The server committed, then the response was lost."""


class IdempotencyConflict(LabError):
    """The export worker saw 409 or 422 for a key it derived itself."""
