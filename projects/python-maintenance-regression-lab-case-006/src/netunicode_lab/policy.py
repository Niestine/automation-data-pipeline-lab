"""Wire-contract constants. Ingest, emit, and the file helper import this module."""

from __future__ import annotations

import unicodedata

from .errors import ProfileError

# Explicit codec and newline. Call sites pass these instead of the process locale.
ENCODING = "utf-8"
ERRORS = "strict"
OPEN_NEWLINE = ""
CSV_DIALECT = "excel"
NFC_FORM = "NFC"
BOM = b"\xef\xbb\xbf"
C1_MIN = 0x80
C1_MAX = 0x9F


def nfc(text: str) -> str:
    return unicodedata.normalize(NFC_FORM, text)


def reject_c1(text: str) -> None:
    for char in text:
        code = ord(char)
        if C1_MIN <= code <= C1_MAX:
            raise ProfileError(
                f"utf-8 profile rejected C1 control U+{code:04X}",
                codec=ENCODING,
            )


def reject_line_break(text: str) -> None:
    """CR and LF belong to the record separator, not to a field."""

    if "\r" in text:
        bad = 0x0D
    elif "\n" in text:
        bad = 0x0A
    else:
        return
    raise ProfileError(
        f"utf-8 profile rejected line break byte 0x{bad:02x} inside a field",
        codec=ENCODING,
        bad_byte=bad,
    )


def reject_unassigned(text: str) -> None:
    version = unicodedata.unidata_version
    for char in text:
        if unicodedata.category(char) == "Cn":
            code = ord(char)
            raise ProfileError(
                f"nfc profile rejected unassigned U+{code:04X}",
                codec="nfc",
                unidata_version=version,
            )


def encode_strict(text: str) -> None:
    try:
        text.encode(ENCODING, ERRORS)
    except UnicodeEncodeError as exc:
        raise ProfileError(
            f"utf-8 strict cannot encode field: {exc.reason}",
            codec=ENCODING,
        ) from exc
