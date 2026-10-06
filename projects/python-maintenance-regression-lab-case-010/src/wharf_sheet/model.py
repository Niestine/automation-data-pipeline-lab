"""Typed parse results. The handler sees records only after acceptance."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepairEvent:
    """One logged repair. Strict mode never emits these."""

    decision_id: str
    record_index: int
    field_index: int
    byte_offset: int


@dataclass(frozen=True)
class ParseSuccess:
    header: tuple[str, ...] | None
    records: tuple[tuple[str, ...], ...]
    events: tuple[RepairEvent, ...]
    bom_stripped: bool


@dataclass(frozen=True)
class ParseFailure:
    code: str
    decision_id: str
    record_index: int | None
    field_index: int | None
    byte_offset: int | None
    events: tuple[RepairEvent, ...]
    bom_stripped: bool


@dataclass(frozen=True)
class Decoded:
    text: str
    offsets: tuple[int, ...]
    end_offset: int
    bom_stripped: bool
    replacement_offsets: tuple[int, ...]


@dataclass(frozen=True)
class DecodeFailure:
    code: str
    decision_id: str
    byte_offset: int


class UnparseError(ValueError):
    """The emitter refuses values it cannot write as strict text."""


class AdmitError(ValueError):
    """A witness may not enter the corpus without a decision id and a bucket."""
