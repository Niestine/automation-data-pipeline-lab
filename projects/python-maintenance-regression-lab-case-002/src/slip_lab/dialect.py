"""SLIP dialect: values, quoting, validity, and metamorphic relations.

Records are sequences of field strings. A successful parse is
``("records", header, rows)``. An error is ``(code,)``. Any extra
message text is display-only and is not part of equality.
"""

from __future__ import annotations

from slip_lab.engine import Policy, raw_field_table, scan

STRICT = Policy(
    name="dialect",
    doubled_quotes=True,
    drop_trailing_empty=False,
    nul_mode="error",
    cr_mode="error",
    hang_seconds=0,
    strip_hash_lines=False,
    reject_hang_token=False,
)

HEADER_MARK = "@slip"

_ERROR_BRANCH = {
    "E_EMPTY_DOCUMENT",
    "E_NUL",
    "E_CARRIAGE",
    "E_HANG_TOKEN",
    "E_UNCLOSED_QUOTE",
    "E_BARE_QUOTE",
    "E_FIELD_COUNT",
}


def is_error(value: object) -> bool:
    return isinstance(value, tuple) and bool(value) and isinstance(value[0], str) and value[0] in _ERROR_BRANCH


def is_records(value: object) -> bool:
    return isinstance(value, tuple) and len(value) == 3 and value[0] == "records"


def _fields(row: object) -> tuple[str, ...]:
    if not isinstance(row, (list, tuple)) or not all(isinstance(field, str) for field in row):
        raise TypeError("a record must be a sequence of field strings")
    return tuple(row)


def canonical(value: object) -> tuple:
    """Equality key. Error codes ignore trailing message text."""

    if isinstance(value, list):
        value = tuple(value)
    if not isinstance(value, tuple) or not value:
        raise TypeError("parser value must be a tuple")
    head = value[0]
    if head == "records":
        if len(value) != 3:
            raise TypeError("records value must be a triple")
        header = value[1]
        if header is not None:
            header = _fields(header)
        if not isinstance(value[2], (list, tuple)):
            raise TypeError("records rows must be a sequence")
        return ("records", header, tuple(_fields(row) for row in value[2]))
    if isinstance(head, str) and head in _ERROR_BRANCH:
        return ("error", head)
    raise TypeError(f"unknown parser value {head!r}")


def same(left: object, right: object) -> bool:
    return canonical(left) == canonical(right)


def is_dialect_valid(blob: bytes) -> bool:
    """Syntactic acceptance. Field-count mismatches stay in the language."""

    try:
        value = scan(bytes(blob), STRICT)
    except Exception:
        return False
    if not isinstance(value, tuple) or not value:
        return False
    if value[0] == "records" or value[0] == "E_FIELD_COUNT":
        return True
    return False


def quote_field(field: str) -> str:
    if field == "" or any(char in field for char in '"|\r\n'):
        return '"' + field.replace('"', '""') + '"'
    return field


def serialize(rows: object, header: object = None) -> bytes:
    """Render records with the single dialect quoter. Always ends in LF."""

    lines: list[str] = []
    if header is not None:
        lines.append("|".join([HEADER_MARK, *[quote_field(str(field)) for field in header]]))
    for row in rows:
        lines.append("|".join(quote_field(str(field)) for field in row))
    text = "\n".join(lines)
    if text:
        text += "\n"
    return text.encode("latin-1")



def allows_permutation(value: object) -> bool:
    """Headerless records have no cross-row state in this dialect.

    A row whose first field is the header mark would become a header if
    it moved to the top, so such a value is not permuted.
    """

    if not is_records(value):
        return False
    _kind, header, rows = canonical(value)
    if any(row and row[0] == HEADER_MARK for row in rows):
        return False
    return header is None and len(rows) >= 2


def boundary_edit(row: tuple[str, ...]) -> tuple[str, str] | None:
    """Swap the characters that touch the first separator.

    Chen's binary-search follow-up probes the keys on either side of a
    split that the original key happened to land on. Here the original
    row is the successful split, and the swapped row is the neighbor.
    """

    if len(row) < 2:
        return None
    left, right = row[0], row[1]
    if not left or not right:
        return None
    if any(char in left + right for char in '"|\r\n'):
        return None
    swapped = (left[:-1] + right[0], left[-1] + right[1:])
    # Each probe puts a field first on its line, where the header mark
    # would change the parse. The relation does not cover that edit.
    if HEADER_MARK in (left, right, swapped[0]):
        return None
    return swapped


def grammar_table(blob: bytes) -> list[list[str]] | None:
    return raw_field_table(bytes(blob), STRICT)
