"""External shortlex shrink.

Passes run in order and repeat until a full cycle accepts nothing:
delete one record, delete one field, delete one character. A candidate
is kept only when the caller-supplied interestingness test accepts it.
Among those, the shortlex-smaller byte string wins. The passes are
local, so the result is a local minimum of the edits explored.
"""

from __future__ import annotations

from collections.abc import Callable

from slip_lab.dialect import grammar_table, is_dialect_valid
from slip_lab.errors import ShrinkError

Interesting = Callable[[bytes], bool]
_STEP_BUDGET = 400


def shortlex_key(blob: bytes) -> tuple[int, bytes]:
    return (len(blob), blob)


def shortlex_smaller(left: bytes, right: bytes) -> bool:
    return shortlex_key(left) < shortlex_key(right)


def _unique(blobs: list[bytes]) -> list[bytes]:
    seen: set[bytes] = set()
    out: list[bytes] = []
    for blob in blobs:
        if blob in seen:
            continue
        seen.add(blob)
        out.append(blob)
    return out


def _split_lines(blob: bytes) -> tuple[list[str], bool]:
    text = blob.decode("latin-1")
    trailing = text.endswith("\n")
    body = text[:-1] if trailing else text
    if body == "":
        return [], trailing
    return body.split("\n"), trailing


def _join_lines(lines: list[str], trailing: bool) -> bytes:
    if not lines:
        return b""
    text = "\n".join(lines)
    if trailing:
        text += "\n"
    return text.encode("latin-1")


def record_candidates(blob: bytes) -> list[bytes]:
    lines, trailing = _split_lines(blob)
    if len(lines) <= 1:
        if lines:
            return [b""]
        return []
    found = []
    for index in range(len(lines)):
        found.append(_join_lines(lines[:index] + lines[index + 1 :], trailing))
    return _unique(found)


def field_candidates(blob: bytes) -> list[bytes]:
    table = grammar_table(blob)
    if not table:
        return []
    _lines, trailing = _split_lines(blob)
    found = []
    for record_index, fields in enumerate(table):
        if len(fields) < 2:
            continue
        for field_index in range(len(fields)):
            edited = [list(row) for row in table]
            del edited[record_index][field_index]
            lines = ["|".join(row) for row in edited]
            found.append(_join_lines(lines, trailing))
    return _unique(found)


def char_candidates(blob: bytes) -> list[bytes]:
    return _unique([blob[:index] + blob[index + 1 :] for index in range(len(blob))])


def iter_candidates(blob: bytes) -> list[bytes]:
    return _unique(record_candidates(blob) + field_candidates(blob) + char_candidates(blob))


def _best(candidates: list[bytes], interesting: Interesting) -> bytes | None:
    accepted = [candidate for candidate in candidates if interesting(candidate)]
    if not accepted:
        return None
    return min(accepted, key=shortlex_key)


def shrink(blob: bytes, interesting: Interesting) -> bytes:
    current = bytes(blob)
    steps = 0
    while True:
        progressed = False
        for producer in (record_candidates, field_candidates, char_candidates):
            while True:
                if steps >= _STEP_BUDGET:
                    raise ShrinkError("shrink step budget exceeded")
                chosen = _best(producer(current), interesting)
                steps += 1
                if chosen is None or not shortlex_smaller(chosen, current):
                    break
                current = chosen
                progressed = True
        if not progressed:
            return current


def is_local_minimum(blob: bytes, interesting: Interesting) -> bool:
    for candidate in iter_candidates(blob):
        if interesting(candidate) and shortlex_smaller(candidate, blob):
            return False
    return True


def make_interesting(parent: bytes, classify) -> Interesting:
    """Same outcome class and side, and the same dialect validity."""

    parent_out = classify(parent)
    parent_valid = is_dialect_valid(parent)

    def interesting(blob: bytes) -> bool:
        if is_dialect_valid(blob) != parent_valid:
            return False
        outcome = classify(blob)
        return (
            outcome.kind == parent_out.kind
            and outcome.side == parent_out.side
            and bool(outcome.truncated) == bool(parent_out.truncated)
        )

    return interesting
