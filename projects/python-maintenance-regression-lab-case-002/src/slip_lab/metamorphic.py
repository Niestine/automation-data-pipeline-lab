"""Follow-up checks derived from a successful parse.

Corpus bytes are never rewritten. Follow-ups are built in memory.
Generated mode samples one permutation and one boundary edit. The
fixed corpus runs the full set. A follow-up that crashes or returns a
value outside the dialect contract is a violation. A surviving relation
is not a proof.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from slip_lab.dialect import (
    allows_permutation,
    boundary_edit,
    canonical,
    is_records,
    serialize,
)


# Every non-identity order up to this many rows; past it, rotations and
# the reversal, so a long record list cannot grow the check factorially.
_MAX_FULL_PERMUTATION_ROWS = 5


@dataclass
class RelationReport:
    violations: int
    skips: int
    notes: list[str]


def _empty() -> RelationReport:
    return RelationReport(0, 0, [])


def _fail(note: str) -> RelationReport:
    return RelationReport(1, 0, [note])


def _holds(parser, blob: bytes, expected: tuple) -> bool:
    try:
        return canonical(parser(blob)) == expected
    except Exception:
        return False


def _orders(count: int, full: bool) -> list[tuple[int, ...]]:
    identity = tuple(range(count))
    if not full:
        return [tuple(reversed(identity))]
    if count <= _MAX_FULL_PERMUTATION_ROWS:
        return [perm for perm in itertools.permutations(identity) if perm != identity]
    rotations = [identity[shift:] + identity[:shift] for shift in range(1, count)]
    return rotations + [tuple(reversed(identity))]


def check_quote_round_trip(parser, value: object) -> RelationReport:
    if not is_records(value):
        return RelationReport(0, 1, ["not-records"])
    _kind, header, rows = canonical(value)
    blob = serialize(rows, header)
    if not _holds(parser, blob, ("records", header, rows)):
        return _fail("quote-round-trip")
    return _empty()


def check_permutation(parser, value: object, *, full: bool) -> RelationReport:
    if not allows_permutation(value):
        return RelationReport(0, 1, ["permutation-skipped"])
    _kind, header, rows = canonical(value)
    violations = 0
    notes: list[str] = []
    for perm in _orders(len(rows), full):
        new_rows = tuple(rows[index] for index in perm)
        blob = serialize(new_rows, None)
        if not _holds(parser, blob, ("records", None, new_rows)):
            violations += 1
            notes.append("permutation:" + ",".join(str(index) for index in perm))
            if not full:
                break
    return RelationReport(violations, 0, notes)


def check_boundary(parser, value: object, *, full: bool) -> RelationReport:
    """Neighbor split plus the two adjacent field probes.

    The relation for an unquoted multi-field row is: the swapped
    characters parse as the literal fields on each side of the moved
    separator, and each adjacent field parses alone as that field.
    """

    if not is_records(value):
        return RelationReport(0, 1, ["not-records"])
    _kind, _header, rows = canonical(value)
    if not rows:
        return RelationReport(0, 1, ["no-rows"])
    row = rows[0]
    edited = boundary_edit(row)
    if edited is None:
        return RelationReport(0, 1, ["boundary-skipped"])
    left, right = edited
    violations = 0
    notes: list[str] = []
    swapped = serialize(((left, right),), None)
    if not _holds(parser, swapped, ("records", None, ((left, right),))):
        violations += 1
        notes.append("boundary-swap")
    if full:
        probes = (row[0], row[1])
    else:
        probes = (row[0],)
    for field in probes:
        probe = serialize(((field,),), None)
        if not _holds(parser, probe, ("records", None, ((field,),))):
            violations += 1
            notes.append("boundary-probe:" + field)
    return RelationReport(violations, 0, notes)


def check_success(parser, value: object, *, full: bool) -> RelationReport:
    parts = (
        check_quote_round_trip(parser, value),
        check_permutation(parser, value, full=full),
        check_boundary(parser, value, full=full),
    )
    return RelationReport(
        sum(part.violations for part in parts),
        sum(part.skips for part in parts),
        [note for part in parts for note in part.notes],
    )
