"""In-memory rows shared by parsing, detection, and injection.

Cell values are already typed. ``None`` means a missing token, not a failed cast.
A failed cast keeps ``None`` and records the reason in ``cast_errors`` so a later
not-null check can tell the two apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional, Union

Atomic = Union[str, Decimal, date, datetime, time, bool, None]


@dataclass
class ViewRow:
    row_id: str
    source_row: int
    output_row: int
    values: dict[str, Atomic]
    raw: dict[str, str]
    notes: dict[str, list[str]] = field(default_factory=dict)
    cast_errors: dict[str, list[str]] = field(default_factory=dict)
    match: dict[str, Optional[str]] = field(default_factory=dict)
    ragged: bool = False
    parse_errors: list[str] = field(default_factory=list)

    def copy(self) -> "ViewRow":
        return ViewRow(
            row_id=self.row_id,
            source_row=self.source_row,
            output_row=self.output_row,
            values=dict(self.values),
            raw=dict(self.raw),
            notes={key: list(value) for key, value in self.notes.items()},
            cast_errors={key: list(value) for key, value in self.cast_errors.items()},
            match=dict(self.match),
            ragged=self.ragged,
            parse_errors=list(self.parse_errors),
        )


@dataclass(frozen=True)
class Hit:
    """One cell involved in one named rule failure."""

    rule_id: str
    kind: str
    row_id: str
    column: str
    detail: str
    source_row: int
    output_row: int
