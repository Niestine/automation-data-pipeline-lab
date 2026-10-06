"""Stage 1. BOM sniff, then fatal decode. No replacement characters."""

from __future__ import annotations

from typing import Optional


# Labels the lab will decode when the supplier names them. windows-1252 is
# accepted only as a declared charset. A missing charset does not select it.
_LABELS = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "utf-16": "utf-16",
    "utf-16be": "utf-16-be",
    "utf-16le": "utf-16-le",
    "cp932": "cp932",
    "shift_jis": "shift_jis",
    "windows-31j": "cp932",
    "windows-1252": "windows-1252",
}


def normalize_label(charset: Optional[str]) -> Optional[str]:
    if charset is None:
        return None
    # Labels match case-insensitively. "shift_jis" folds to "shift-jis" here,
    # so both spellings are listed. Unknown labels return None and fail closed.
    key = charset.strip().lower()
    return _LABELS.get(key) or _LABELS.get(key.replace("_", "-"))


def sniff_bom(payload: bytes) -> Optional[tuple]:
    """Return (encoding, consumed_bytes) using Encoding Standard BOM order."""
    if payload.startswith(b"\xef\xbb\xbf"):
        return ("utf-8", 3)
    if payload.startswith(b"\xfe\xff"):
        return ("utf-16-be", 2)
    if payload.startswith(b"\xff\xfe"):
        return ("utf-16-le", 2)
    return None


def decode_bytes(payload: bytes, declared_charset: Optional[str]) -> dict:
    bom = sniff_bom(payload)
    overridden = False
    if bom is not None:
        encoding, consumed = bom
        body = payload[consumed:]
        declared = normalize_label(declared_charset)
        # A bare "utf-16" label agrees with either UTF-16 BOM; the BOM only
        # supplies the byte order the label left open.
        agrees = declared == encoding or (declared == "utf-16" and encoding.startswith("utf-16"))
        if declared is not None and not agrees:
            overridden = True
    else:
        encoding = normalize_label(declared_charset)
        body = payload
        if encoding is None:
            return {
                "ok": False,
                "reason": "charset_missing",
                "text": None,
                "encoding": None,
                "charset_overridden_by_bom": False,
            }
    if encoding == "utf-16":
        # Endianness is not knowable without a BOM. Do not guess.
        return {
            "ok": False,
            "reason": "decode_fatal",
            "text": None,
            "encoding": encoding,
            "charset_overridden_by_bom": overridden,
            "detail": "utf-16 declared without BOM",
        }
    try:
        text = body.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError):
        return {
            "ok": False,
            "reason": "decode_fatal",
            "text": None,
            "encoding": encoding,
            "charset_overridden_by_bom": overridden,
        }
    if "\x00" in text:
        return {
            "ok": False,
            "reason": "binary_payload",
            "text": None,
            "encoding": encoding,
            "charset_overridden_by_bom": overridden,
        }
    return {
        "ok": True,
        "reason": None,
        "text": text,
        "encoding": encoding,
        "charset_overridden_by_bom": overridden,
    }
