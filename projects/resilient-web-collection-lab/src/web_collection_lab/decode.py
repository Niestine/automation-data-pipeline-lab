"""HTML byte decoding: Content-Type charset, meta charset, then fallbacks."""

from __future__ import annotations

import re

_CHARSET_HEADER = re.compile(r"charset\s*=\s*['\"]?([A-Za-z0-9_.:-]+)", re.I)
_META_CHARSET = re.compile(
    br"<meta\b[^>]*?charset\s*=\s*['\"]?([A-Za-z0-9_.:-]+)",
    re.I,
)
_META_HTTP_EQUIV = re.compile(
    br"<meta\b[^>]*http-equiv\s*=\s*['\"]?content-type['\"][^>]*charset\s*=\s*['\"]?([A-Za-z0-9_.:-]+)",
    re.I,
)


def charset_from_content_type(content_type: str) -> str | None:
    match = _CHARSET_HEADER.search(content_type or "")
    if match is None:
        return None
    return match.group(1).strip().lower()


def sniff_meta_charset(body: bytes) -> str | None:
    head = body[:2048]
    match = _META_CHARSET.search(head) or _META_HTTP_EQUIV.search(head)
    if match is None:
        return None
    return match.group(1).decode("ascii", errors="ignore").lower()


def decode_html(body: bytes, content_type: str = "") -> tuple[str, str]:
    """Return `(text, charset_used)`.

    Preference: Content-Type charset, then `<meta charset>`, then utf-8,
    then iso-8859-1. latin-1 is the last-resort decode that never fails.
    """
    candidates: list[str] = []
    header = charset_from_content_type(content_type)
    if header:
        candidates.append(header)
    meta = sniff_meta_charset(body)
    if meta and meta not in candidates:
        candidates.append(meta)
    for enc in ("utf-8", "utf-8-sig", "iso-8859-1"):
        if enc not in candidates:
            candidates.append(enc)
    for enc in candidates:
        try:
            return body.decode(enc), enc
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("latin-1"), "latin-1"
