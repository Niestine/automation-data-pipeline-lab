"""Stage 3. RFC 4180 records with the dialect's delimiter, quote, and break.

Spaces are data. A trailing delimiter quarantines the row. Quoted-empty and
omitted-empty both yield an empty string and keep a syntactic annotation.
"""

from __future__ import annotations

from typing import Optional


_BREAKS = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}


def line_break_text(name: str) -> str:
    return _BREAKS[name]


def detect_line_break(text: str) -> str:
    if "\r\n" in text:
        return "crlf"
    if "\n" in text:
        return "lf"
    if "\r" in text:
        return "cr"
    return "lf"


def parse_text(text: str, delimiter: str, quote: str, escape: str, line_break: str) -> list:
    if delimiter == "":
        raise ValueError("delimiter is required")
    lb = line_break_text(line_break)
    rows = []
    index = 0
    length = len(text)
    line_no = 1
    while index < length:
        start_line = line_no
        fields = []
        kinds = []
        buf = []
        in_quotes = False
        quoted = False
        closed = False
        field_start = True
        reason = None
        trailing = False
        consumed = False
        while index < length:
            if not in_quotes and text.startswith(lb, index):
                index += len(lb)
                line_no += 1
                break
            char = text[index]
            consumed = True
            if quote and field_start and char == quote:
                in_quotes = True
                quoted = True
                field_start = False
                index += 1
                continue
            if in_quotes:
                if escape and char == escape and index + 1 < length:
                    nxt = text[index + 1]
                    buf.append(nxt)
                    if nxt == "\n":
                        line_no += 1
                    index += 2
                    continue
                if char == quote:
                    if not escape and index + 1 < length and text[index + 1] == quote:
                        buf.append(quote)
                        index += 2
                        continue
                    in_quotes = False
                    closed = True
                    index += 1
                    continue
                if char == "\n":
                    line_no += 1
                buf.append(char)
                index += 1
                continue
            if char == delimiter:
                kinds.append("quoted" if quoted else ("omitted" if not buf else "value"))
                fields.append("".join(buf))
                buf = []
                quoted = False
                closed = False
                field_start = True
                if index + 1 >= length or text.startswith(lb, index + 1):
                    trailing = True
                index += 1
                continue
            if quote and (char == quote or closed):
                # A quote inside an unquoted field, or text after a closing
                # quote, is outside the RFC 4180 grammar.
                reason = "bad_quote"
            field_start = False
            buf.append(char)
            index += 1
        if in_quotes and reason is None:
            reason = "bad_quote"
        if not consumed and not fields and not buf:
            continue
        if trailing and not buf and not quoted:
            reason = reason or "trailing_delimiter"
        else:
            kinds.append("quoted" if quoted else ("omitted" if not buf else "value"))
            fields.append("".join(buf))
        rows.append(
            {
                "source_row": start_line,
                "fields": fields,
                "kinds": kinds,
                "reason": reason,
            }
        )
    return rows


def syntactic_empty(kind: str, value: str) -> Optional[str]:
    if value != "":
        return None
    if kind == "quoted":
        return "quoted"
    if kind == "omitted":
        return "omitted"
    return None
