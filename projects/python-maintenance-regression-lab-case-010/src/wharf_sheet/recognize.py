"""Recognizer. The handler runs only after the whole input is accepted.

hardened_parse is the default. legacy_parse stays callable for the compatibility
window. Strict successes carry an empty event tuple. Legacy repairs carry a
decision id, a record index, a field index, and a byte offset.
"""

from __future__ import annotations

from collections.abc import Callable

from .decode import decode_bytes
from .logsetup import log_result
from .model import DecodeFailure, Decoded, ParseFailure, ParseSuccess, RepairEvent

DEFAULT_FIELD_LIMIT = 4096
Handler = Callable[[tuple[tuple[str, ...], ...]], None]


class _Fail(Exception):
    def __init__(
        self,
        code: str,
        decision_id: str,
        record_index: int | None,
        field_index: int | None,
        byte_offset: int | None,
    ) -> None:
        self.code = code
        self.decision_id = decision_id
        self.record_index = record_index
        self.field_index = field_index
        self.byte_offset = byte_offset


def hardened_parse(
    data: bytes,
    *,
    header: str = "absent",
    charset: str = "utf-8",
    field_limit: int = DEFAULT_FIELD_LIMIT,
    handler: Handler | None = None,
    mutant: str | None = None,
) -> ParseSuccess | ParseFailure:
    return _parse(
        data,
        header=header,
        charset=charset,
        field_limit=field_limit,
        handler=handler,
        profile="hardened",
        mutant=mutant,
    )


parse = hardened_parse


def legacy_parse(
    data: bytes,
    *,
    header: str = "absent",
    charset: str = "utf-8",
    field_limit: int = DEFAULT_FIELD_LIMIT,
    handler: Handler | None = None,
    mutant: str | None = None,
) -> ParseSuccess | ParseFailure:
    return _parse(
        data,
        header=header,
        charset=charset,
        field_limit=field_limit,
        handler=handler,
        profile="legacy",
        mutant=mutant,
    )


def _parse(
    data: bytes,
    *,
    header: str,
    charset: str,
    field_limit: int,
    handler: Handler | None,
    profile: str,
    mutant: str | None,
) -> ParseSuccess | ParseFailure:
    if header not in {"present", "absent"}:
        raise ValueError("header must be 'present' or 'absent'")
    if not isinstance(field_limit, int) or isinstance(field_limit, bool) or field_limit < 1:
        raise ValueError("field_limit must be a positive int")
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    raw = bytes(data)
    legacy = profile == "legacy"
    policy = "replacement" if legacy else "fatal"
    decode_mutant = mutant if legacy else None
    scan_mutant = mutant if not legacy else None

    decoded = decode_bytes(raw, policy=policy, charset=charset, mutant=decode_mutant)
    if isinstance(decoded, DecodeFailure):
        failure = ParseFailure(
            code=decoded.code,
            decision_id=decoded.decision_id,
            record_index=None,
            field_index=None,
            byte_offset=decoded.byte_offset,
            events=(),
            bom_stripped=False,
        )
        log_result(failure)
        return failure

    bom_event: tuple[RepairEvent, ...] = ()
    if decoded.bom_stripped:
        bom_event = (RepairEvent("D-bom", 0, 0, 0),)

    try:
        header_values, records, events = _scan(
            decoded,
            header=header,
            field_limit=field_limit,
            legacy=legacy,
            mutant=scan_mutant,
        )
    except _Fail as exc:
        failure = ParseFailure(
            code=exc.code,
            decision_id=exc.decision_id,
            record_index=exc.record_index,
            field_index=exc.field_index,
            byte_offset=exc.byte_offset,
            events=bom_event,
            bom_stripped=decoded.bom_stripped,
        )
        log_result(failure)
        return failure

    success = ParseSuccess(
        header=header_values,
        records=records,
        events=bom_event + events,
        bom_stripped=decoded.bom_stripped,
    )
    if not legacy and success.events:
        # Strict mode has no repair path. A stray event is a recognizer bug.
        raise RuntimeError("strict parse emitted a repair event")
    if handler is not None:
        handler(success.records)
    log_result(success)
    return success


def _over_limit(length: int, limit: int, mutant: str | None) -> bool:
    if mutant == "limit_boundary_swap":
        return length >= limit
    return length > limit


def _counts_differ(got: int, expected: int, mutant: str | None) -> bool:
    if mutant == "field_count_swap":
        return got == expected
    return got != expected


def _is_comma(ch: str, mutant: str | None) -> bool:
    if mutant == "semicolon_comma":
        return ch in {",", ";"}
    return ch == ","


def _scan(
    decoded: Decoded,
    *,
    header: str,
    field_limit: int,
    legacy: bool,
    mutant: str | None,
) -> tuple[tuple[str, ...] | None, tuple[tuple[str, ...], ...], tuple[RepairEvent, ...]]:
    text = decoded.text
    offsets = decoded.offsets
    end_offset = decoded.end_offset
    replacements = set(decoded.replacement_offsets)
    n = len(text)
    if len(offsets) != n:
        raise RuntimeError("decode offset map does not match the text")

    def byte_at(index: int) -> int:
        if index < n:
            return offsets[index]
        return end_offset

    if n == 0:
        raise _Fail("E-empty", "D-empty", None, None, end_offset)

    records: list[list[str]] = []
    events: list[RepairEvent] = []
    record_starts: list[int] = []
    header_field_bytes: list[int] = []
    i = 0

    while i < n:
        if text[i] == "#":
            record_index = len(records)
            if not legacy:
                raise _Fail("E-comment", "D-comments", record_index, 0, byte_at(i))
            start = i
            while i < n and text[i] not in "\r\n":
                i += 1
            events.append(RepairEvent("D-comments", record_index, 0, byte_at(start)))
            if i < n and text[i] == "\r" and i + 1 < n and text[i + 1] == "\n":
                i += 2
            elif i < n and text[i] in "\r\n":
                i += 1
            continue

        record_index = len(records)
        record_starts.append(byte_at(i))
        fields, i, rec_events = _read_record(
            text,
            i,
            record_index=record_index,
            field_limit=field_limit,
            legacy=legacy,
            mutant=mutant,
            byte_at=byte_at,
            replacements=replacements,
            capture_field_bytes=record_index == 0,
            header_field_bytes=header_field_bytes,
        )
        events.extend(rec_events)
        records.append(fields)

    if not records:
        raise _Fail("E-empty", "D-empty", None, None, end_offset)

    width = len(records[0])
    for idx in range(1, len(records)):
        got = len(records[idx])
        if _counts_differ(got, width, mutant):
            if not legacy:
                diverge = got if got < width else width
                raise _Fail("E-ragged", "D-ragged", idx, diverge, record_starts[idx])
            if got < width:
                records[idx] = records[idx] + [""] * (width - got)
                events.append(RepairEvent("D-ragged", idx, got, record_starts[idx]))
            else:
                events.append(RepairEvent("D-ragged", idx, width, record_starts[idx]))

    if header == "absent":
        data = tuple(tuple(row) for row in records)
        return None, data, tuple(events)

    header_values = tuple(records[0])
    data_rows = records[1:]
    if not data_rows:
        raise _Fail("E-empty", "D-empty", 0, None, record_starts[0])
    seen: set[str] = set()
    for field_index, name in enumerate(header_values):
        if name in seen:
            at = header_field_bytes[field_index] if field_index < len(header_field_bytes) else record_starts[0]
            if not legacy:
                raise _Fail("E-dup-header", "D-dup-header", 0, field_index, at)
            # One event per repeated name, so no accepted duplicate is silent.
            events.append(RepairEvent("D-dup-header", 0, field_index, at))
        seen.add(name)
    data = tuple(tuple(row) for row in data_rows)
    return header_values, data, tuple(events)


def _read_record(
    text: str,
    i: int,
    *,
    record_index: int,
    field_limit: int,
    legacy: bool,
    mutant: str | None,
    byte_at: Callable[[int], int],
    replacements: set[int],
    capture_field_bytes: bool,
    header_field_bytes: list[int],
) -> tuple[list[str], int, list[RepairEvent]]:
    n = len(text)
    fields: list[str] = []
    field: list[str] = []
    events: list[RepairEvent] = []
    field_index = 0
    in_quotes = False
    in_single = False
    closed_quote = False
    quote_open_at: int | None = None
    if capture_field_bytes:
        header_field_bytes.append(byte_at(i))

    def push(ch: str, at_index: int) -> None:
        field.append(ch)
        if _over_limit(len(field), field_limit, mutant):
            raise _Fail("E-limit", "D-limit", record_index, field_index, byte_at(at_index))
        if ch == "\ufffd" and byte_at(at_index) in replacements:
            events.append(RepairEvent("D-decode", record_index, field_index, byte_at(at_index)))

    def finish() -> None:
        fields.append("".join(field))

    while i < n:
        ch = text[i]
        if in_quotes:
            if ch == '"':
                doubled = mutant != "drop_doubled_quote" and i + 1 < n and text[i + 1] == '"'
                if doubled:
                    push('"', i)
                    i += 2
                    continue
                in_quotes = False
                closed_quote = True
                i += 1
                continue
            push(ch, i)
            i += 1
            continue

        if in_single:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    push("'", i)
                    i += 2
                    continue
                in_single = False
                closed_quote = True
                i += 1
                continue
            push(ch, i)
            i += 1
            continue

        if _is_comma(ch, mutant):
            finish()
            field = []
            field_index += 1
            closed_quote = False
            quote_open_at = None
            i += 1
            if capture_field_bytes:
                header_field_bytes.append(byte_at(i))
            continue

        if ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                finish()
                return fields, i + 2, events
            if not legacy:
                raise _Fail("E-bare-cr", "D-crlf", record_index, field_index, byte_at(i))
            events.append(RepairEvent("D-crlf", record_index, field_index, byte_at(i)))
            finish()
            return fields, i + 1, events

        if ch == "\n":
            if not legacy:
                raise _Fail("E-bare-lf", "D-crlf", record_index, field_index, byte_at(i))
            events.append(RepairEvent("D-crlf", record_index, field_index, byte_at(i)))
            finish()
            return fields, i + 1, events

        if ch == '"':
            if field or closed_quote:
                code = "E-quote-tail" if closed_quote else "E-bare-quote"
                raise _Fail(code, "D-quotes", record_index, field_index, byte_at(i))
            in_quotes = True
            quote_open_at = i
            i += 1
            continue

        if closed_quote:
            # Text after a closing quote fails in both profiles, before any escape handling.
            raise _Fail("E-quote-tail", "D-quotes", record_index, field_index, byte_at(i))

        if ch == "'" and not field:
            if not legacy:
                raise _Fail("E-single-quote", "D-quotes", record_index, field_index, byte_at(i))
            in_single = True
            events.append(RepairEvent("D-quotes", record_index, field_index, byte_at(i)))
            i += 1
            continue

        if ch == "\\" and i + 1 < n and text[i + 1] in ",\"'\r\n\\":
            if not legacy:
                raise _Fail("E-backslash", "D-quotes", record_index, field_index, byte_at(i))
            events.append(RepairEvent("D-quotes", record_index, field_index, byte_at(i)))
            push(text[i + 1], i + 1)
            i += 2
            continue

        push(ch, i)
        i += 1

    if in_quotes:
        if mutant == "drop_unclosed":
            finish()
            return fields, n, events
        at = byte_at(quote_open_at if quote_open_at is not None else i)
        raise _Fail("E-unclosed", "D-quotes", record_index, field_index, at)
    if in_single:
        raise _Fail("E-unclosed", "D-quotes", record_index, field_index, byte_at(i))
    finish()
    return fields, n, events
