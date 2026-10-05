"""Exceptions for the ledger recovery lab."""

from __future__ import annotations


class RecoveryError(Exception):
    """Base error for recovery, publish, and migration failures."""


class StructuralCorruption(RecoveryError):
    """A checksum hole sits inside an otherwise framed log prefix.

    The files are left unchanged. A torn tail (a short read at EOF) is not
    this error; that tail is dropped.
    """

    def __init__(self, message: str, *, lsn: int, last_durable_lsn: int) -> None:
        super().__init__(message)
        self.lsn = lsn
        self.last_durable_lsn = last_durable_lsn


class UnavailableSchemaGap(RecoveryError):
    """Stored schema and this process differ by more than one version.

    Opening raises and does not rewrite the snapshot or the log. The same
    error is used when an unknown log record is required to rebuild a
    durable commit.
    """

    def __init__(
        self,
        message: str,
        *,
        code_version: int,
        stored_version: int,
        last_durable_lsn: int,
    ) -> None:
        super().__init__(message)
        self.code_version = code_version
        self.stored_version = stored_version
        self.last_durable_lsn = last_durable_lsn


class SameVolumeRequired(RecoveryError):
    """Temp file and live snapshot are not on the same device.

    Raised before the destination is opened for write or delete.
    """


class SimulatedCrash(RecoveryError):
    """In-process stand-in for a crash before a log force."""


class MigrationError(RecoveryError):
    """Rejected schema transition."""


class ConstraintError(RecoveryError):
    """A single-row write violated a public constraint or the row shape."""


class IllegalRead(RecoveryError):
    """A reader consulted an element that is not public."""
