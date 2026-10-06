"""The only writer. Callers pass strings; this module emits strict UTF-8 CSV.

Output uses comma, CRLF, doubled quotes, no BOM, and no backslash escapes.
None is not written as an empty field.
"""

from __future__ import annotations

from pathlib import Path

from .model import UnparseError

_SPECIAL = ",\"\r\n"


def unparse(
    header: tuple[str, ...] | list[str] | None,
    records: tuple[tuple[str, ...] | list[str], ...] | list[tuple[str, ...] | list[str]],
) -> bytes:
    rows: list[tuple[str, ...] | list[str]] = []
    if header is not None:
        rows.append(header)
    rows.extend(records)
    if not rows:
        raise UnparseError("E-empty")
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) == 0:
            raise UnparseError("E-row")
        cells: list[str] = []
        for cell in row:
            if not isinstance(cell, str):
                raise UnparseError("E-type")
            cells.append(_quote(cell))
        lines.append(",".join(cells))
    text = "\r\n".join(lines) + "\r\n"
    return text.encode("utf-8")


def write_sheet(
    path: str | Path,
    header: tuple[str, ...] | list[str] | None,
    records: tuple[tuple[str, ...] | list[str], ...] | list[tuple[str, ...] | list[str]],
) -> None:
    """Write strict bytes through a text file opened with newline disabled.

    newline='' keeps a quoted LF from being translated again on Windows.
    """

    data = unparse(header, records)
    text = data.decode("utf-8")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _quote(cell: str) -> str:
    if any(ch in cell for ch in _SPECIAL):
        return '"' + cell.replace('"', '""') + '"'
    return cell
