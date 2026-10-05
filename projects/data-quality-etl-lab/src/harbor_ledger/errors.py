"""Typed failures for the harbor-ledger intake pipeline."""

from __future__ import annotations


class HarborError(Exception):
    """Base error. Callers map this to a non-zero exit when the run cannot seal a report."""


class DecodeError(HarborError):
    """The declared encoding label is outside the allow-list."""


class FatalDecodeError(DecodeError):
    """A pipeline-owned UTF-8 artifact failed a strict decode."""


class SchemaError(HarborError):
    """The table schema is missing a required key or asks for an unsupported feature."""


class JCSError(HarborError):
    """Canonical JSON serialization refused the value."""


class InjectionError(HarborError):
    """The clean fixture already violates a rule, so injection must not start."""
