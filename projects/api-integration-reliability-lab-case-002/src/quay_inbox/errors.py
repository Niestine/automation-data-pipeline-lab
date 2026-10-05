"""Lab exceptions. HTTP handlers map AuthError to a generic 401."""

from __future__ import annotations


class AuthError(Exception):
    """Signature, freshness, or route-secret failure.

    ``code`` is for in-process tests. The HTTP body must not include it.
    """

    def __init__(self, code: str) -> None:
        super().__init__("unauthorized")
        self.code = code


class SchemaError(Exception):
    """Payload failed the schema selected by its own api_version."""


class GapError(Exception):
    """Event-list checkpoint is older than the 30-day retention window."""


class SsrfError(Exception):
    """Callback URL failed the https and non-public address checks."""


class ApiUsageError(Exception):
    """Caller combined mutually exclusive list parameters."""


class SimulatedCrash(Exception):
    """Injected crash after an external send and before the completion mark."""

    def __init__(self, event_id: str) -> None:
        super().__init__(event_id)
        self.event_id = event_id
