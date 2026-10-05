"""Failures for the strict UTF-8 checker and the interchange profile."""

from __future__ import annotations


class LabError(Exception):
    """Base error for encoding, profile, and portability failures."""


class StrictUtf8Error(LabError):
    """Bytes are not the single shortest UTF-8 encoding of a scalar."""

    def __init__(self, message: str, *, bad_byte: int, offset: int) -> None:
        super().__init__(message)
        self.bad_byte = bad_byte
        self.offset = offset
        self.codec = "strict-utf8"


class LeadingBomError(StrictUtf8Error):
    """A leading EF BB BF signature is forbidden on this interchange path."""

    def __init__(self) -> None:
        super().__init__(
            "strict-utf8 rejected leading BOM byte 0xef",
            bad_byte=0xEF,
            offset=0,
        )


class ProfileError(LabError):
    """A field or a finished file breaks the Net-Unicode CSV profile."""

    def __init__(
        self,
        message: str,
        *,
        codec: str,
        bad_byte: int | None = None,
        unidata_version: str | None = None,
    ) -> None:
        super().__init__(message)
        self.codec = codec
        self.bad_byte = bad_byte
        self.unidata_version = unidata_version


class CasefoldCollisionError(LabError):
    """Two directory entries match under casefold and would collide on Windows."""

    def __init__(self, candidate: str, existing: str) -> None:
        super().__init__(
            f"casefold collision: candidate {candidate!r} matches existing {existing!r}"
        )
        self.candidate = candidate
        self.existing = existing
