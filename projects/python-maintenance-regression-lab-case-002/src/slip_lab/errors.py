"""Stable failure types for the SLIP maintenance lab."""

from __future__ import annotations


class LabError(Exception):
    """Base error for expected lab failures."""


class LedgerError(LabError):
    """A ledger row is missing a required decision or is malformed."""


class LedgerConflict(LedgerError):
    """The same input bytes were recorded with two different rows."""


class CorpusDriftError(LabError):
    """Published corpus bytes or the ledger file differ from the embedded corpus."""


class GateError(LabError):
    """A compatibility disposition does not match a fresh classification."""


class LogConfigError(LabError):
    """Logging could not be configured before any parser ran."""


class ShrinkError(LabError):
    """The reducer exceeded its step budget."""
