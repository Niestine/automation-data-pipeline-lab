"""BOM-first decoding for a short encoding allow-list.

Supplier bytes follow the Encoding Standard's replacement mode. A leading BOM
wins over the declared label and is consumed. Labels ``ascii`` and ``latin1``
select windows-1252, so byte 0x80 is U+20AC. Pipeline-owned artifacts are
UTF-8 without a BOM; one illegal byte aborts the run.

The output encoding of every artifact this pipeline writes is UTF-8. The HTML
error mode is not used: it would emit numeric character references that look
like supplier data.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .errors import DecodeError, FatalDecodeError

# WHATWG windows-1252 maps five bytes that Python's cp1252 codec rejects.
_CP1252_EXTRA = {
    0x81: "\u0081",
    0x8D: "\u008D",
    0x8F: "\u008F",
    0x90: "\u0090",
    0x9D: "\u009D",
}

_LABELS = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "windows-1252": "windows-1252",
    "ascii": "windows-1252",
    "us-ascii": "windows-1252",
    "latin1": "windows-1252",
    "iso-8859-1": "windows-1252",
    "iso8859-1": "windows-1252",
    "shift-jis": "shift_jis",
    "sjis": "shift_jis",
    "csshiftjis": "shift_jis",
}

_UNICODE_ENCODINGS = {"utf-8", "utf-16be", "utf-16le"}


@dataclass(frozen=True)
class DecodeResult:
    text: str
    encoding_used: str
    declared: str
    bom: bool
    bom_override: bool
    replacement_count: int
    quarantined: bool


def output_encoding(label: str) -> str:
    """Map any source label, including UTF-16 and the replacement encoding, to UTF-8."""

    return "utf-8"


def canonical_label(label: str) -> str:
    key = label.strip().lower().replace("_", "-")
    if key not in _LABELS:
        raise DecodeError(f"encoding label {label!r} is outside the allow-list")
    return _LABELS[key]


def decode_owned(data: bytes) -> str:
    """UTF-8 decode without BOM or fail. Replacement characters are never inserted."""

    if data.startswith((b"\xef\xbb\xbf", b"\xfe\xff", b"\xff\xfe")):
        raise FatalDecodeError("pipeline-owned file must be UTF-8 without a BOM")
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise FatalDecodeError("pipeline-owned file is not strict UTF-8") from exc


def decode_supplier(data: bytes, declared: str, replacement_threshold: int) -> DecodeResult:
    """Decode supplier bytes. Quarantine when U+FFFD count exceeds the threshold."""

    label = canonical_label(declared)
    bom_name, body, bom = _split_bom(data)
    if bom_name is not None:
        encoding_used = bom_name
        bom_override = label != bom_name
    else:
        encoding_used = label
        bom_override = False
    text = _decode_body(body, encoding_used)
    if encoding_used not in _UNICODE_ENCODINGS:
        text = unicodedata.normalize("NFC", text)
    replacements = text.count("\ufffd")
    return DecodeResult(
        text=text,
        encoding_used=encoding_used,
        declared=label,
        bom=bom,
        bom_override=bom_override,
        replacement_count=replacements,
        quarantined=replacements > replacement_threshold,
    )


def _split_bom(data: bytes) -> tuple[str | None, bytes, bool]:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8", data[3:], True
    if data.startswith(b"\xfe\xff"):
        return "utf-16be", data[2:], True
    if data.startswith(b"\xff\xfe"):
        return "utf-16le", data[2:], True
    return None, data, False


def _decode_body(data: bytes, encoding: str) -> str:
    if encoding == "utf-8":
        return data.decode("utf-8", "replace")
    if encoding == "utf-16be":
        return data.decode("utf-16-be", "replace")
    if encoding == "utf-16le":
        return data.decode("utf-16-le", "replace")
    if encoding == "windows-1252":
        return _decode_windows_1252(data)
    if encoding == "shift_jis":
        # CPython's shift_jis replacement mode unmasks an ASCII trail: the
        # illegal pair 0x82 0x22 becomes U+FFFD U+0022, so a quote cannot hide
        # inside a lead byte. That is the Encoding Standard's no-mask rule.
        return data.decode("shift_jis", "replace")
    raise DecodeError(f"no decoder for {encoding}")


def _decode_windows_1252(data: bytes) -> str:
    parts: list[str] = []
    chunk = bytearray()

    def flush() -> None:
        if chunk:
            parts.append(bytes(chunk).decode("cp1252"))
            chunk.clear()

    for byte in data:
        extra = _CP1252_EXTRA.get(byte)
        if extra is None:
            chunk.append(byte)
        else:
            flush()
            parts.append(extra)
    flush()
    return "".join(parts)
