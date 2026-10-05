"""Closed CSV profile.

The writer closes every choice RFC 4180 leaves open: UTF-8, a header, CRLF
between records, a trailing CRLF, and the same field count on every record.
A field is quoted only when it contains comma, CR, LF, or a double quote.
Spaces are data. The reader still accepts a missing final break and LF endings,
and it numbers physical records separately from emitted rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import HarborError


class CsvProfileError(HarborError):
    """The writer refused a row that would break the closed profile."""


@dataclass(frozen=True)
class Dialect:
    delimiter: str = ","
    quote: str = '"'
    header: bool = True
    skip_rows: int = 0
    skip_blank_rows: bool = True
    comment_prefix: str | None = None
    trim: bool = False
    encoding: str = "utf-8"

    def __post_init__(self) -> None:
        if len(self.delimiter) != 1 or len(self.quote) != 1:
            raise CsvProfileError("delimiter and quote must be single characters")
        if self.delimiter == self.quote:
            raise CsvProfileError("delimiter and quote must differ")
        if self.skip_rows < 0:
            raise CsvProfileError("skip_rows must be >= 0")


@dataclass
class RawRecord:
    source_row: int
    output_row: int | None
    kind: str
    fields: list[str] = field(default_factory=list)
    trims: list[dict[str, str | int]] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)


@dataclass
class ParsedCsv:
    header: list[str] | None
    records: list[RawRecord]
    physical: list[RawRecord]


def write_csv(header: list[str], rows: list[list[str]]) -> bytes:
    """Serialize the closed profile. Two calls return identical bytes."""

    width = len(header)
    if width == 0:
        raise CsvProfileError("header must name at least one column")
    lines = [_format_record(header)]
    for row in rows:
        if len(row) != width:
            raise CsvProfileError("every record must have the same field count")
        lines.append(_format_record(row))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def parse_csv(text: str, dialect: Dialect, field_count: int) -> ParsedCsv:
    """Parse ``text`` with an explicit dialect. ``field_count`` is the schema width."""

    if field_count < 1:
        raise CsvProfileError("field_count must be positive")
    physical_text = _split_records(text, dialect.delimiter, dialect.quote)
    physical: list[RawRecord] = []
    records: list[RawRecord] = []
    header: list[str] | None = None
    output_row = 0
    seen_header = False
    for index, record_text in enumerate(physical_text, start=1):
        if index <= dialect.skip_rows:
            physical.append(RawRecord(source_row=index, output_row=None, kind="skipped"))
            continue
        if dialect.comment_prefix and record_text.startswith(dialect.comment_prefix):
            physical.append(RawRecord(source_row=index, output_row=None, kind="comment"))
            continue
        if dialect.skip_blank_rows and record_text == "":
            physical.append(RawRecord(source_row=index, output_row=None, kind="blank"))
            continue
        fields, trims, errors = _parse_fields(record_text, dialect)
        if dialect.header and not seen_header:
            header = fields
            item = RawRecord(
                source_row=index,
                output_row=None,
                kind="header",
                fields=fields,
                trims=trims,
                parse_errors=errors,
            )
            if len(fields) != field_count:
                item.parse_errors.append("ragged-header")
            physical.append(item)
            seen_header = True
            continue
        output_row += 1
        item = RawRecord(
            source_row=index,
            output_row=output_row,
            kind="data",
            fields=fields,
            trims=trims,
            parse_errors=list(errors),
        )
        if len(fields) != field_count:
            item.parse_errors.append("ragged")
            item.kind = "ragged"
        physical.append(item)
        records.append(item)
    return ParsedCsv(header=header, records=records, physical=physical)


def _format_record(fields: list[str]) -> str:
    return ",".join(_quote_field(field) for field in fields)


def _quote_field(value: str) -> str:
    if any(character in value for character in ",\r\n\""):
        return '"' + value.replace('"', '""') + '"'
    return value


def _split_records(text: str, delimiter: str, quote: str) -> list[str]:
    if text == "":
        return []
    records: list[str] = []
    buffer: list[str] = []
    in_quotes = False
    field_start = True
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if in_quotes:
            buffer.append(character)
            if character == quote:
                if index + 1 < length and text[index + 1] == quote:
                    buffer.append(text[index + 1])
                    index += 2
                    continue
                in_quotes = False
            index += 1
            continue
        if character == quote and field_start:
            in_quotes = True
            field_start = False
            buffer.append(character)
            index += 1
            continue
        if character == "\r":
            records.append("".join(buffer))
            buffer = []
            field_start = True
            index += 2 if index + 1 < length and text[index + 1] == "\n" else 1
            continue
        if character == "\n":
            records.append("".join(buffer))
            buffer = []
            field_start = True
            index += 1
            continue
        if character == delimiter:
            field_start = True
        else:
            field_start = False
        buffer.append(character)
        index += 1
    if in_quotes:
        records.append("".join(buffer))
    elif text[-1] not in "\r\n":
        records.append("".join(buffer))
    return records


def _parse_fields(
    record: str, dialect: Dialect
) -> tuple[list[str], list[dict[str, str | int]], list[str]]:
    fields: list[str] = []
    trims: list[dict[str, str | int]] = []
    errors: list[str] = []
    index = 0
    length = len(record)
    while True:
        value, index, trim_note, field_errors = _read_field(record, index, dialect)
        if trim_note is not None:
            trims.append(
                {
                    "index": len(fields),
                    "before": trim_note[0],
                    "after": trim_note[1],
                }
            )
        fields.append(value)
        errors.extend(field_errors)
        if index >= length:
            break
        if record[index] != dialect.delimiter:
            errors.append("delimiter")
            break
        index += 1
        if index == length:
            fields.append("")
            break
    return fields, trims, errors


def _read_field(
    record: str, index: int, dialect: Dialect
) -> tuple[str, int, tuple[str, str] | None, list[str]]:
    length = len(record)
    errors: list[str] = []
    if index < length and record[index] == dialect.quote:
        index += 1
        buffer: list[str] = []
        closed = False
        while index < length:
            character = record[index]
            if character == dialect.quote:
                if index + 1 < length and record[index + 1] == dialect.quote:
                    buffer.append(dialect.quote)
                    index += 2
                    continue
                index += 1
                closed = True
                break
            buffer.append(character)
            index += 1
        if not closed:
            errors.append("unterminated-quote")
        text = "".join(buffer)
    else:
        buffer = []
        while index < length and record[index] != dialect.delimiter:
            buffer.append(record[index])
            index += 1
        text = "".join(buffer)
    if not dialect.trim:
        return text, index, None, errors
    stripped = text.strip(" \t")
    if stripped == text:
        return text, index, None, errors
    return stripped, index, (text, stripped), errors
