"""Classify one response against the copy already stored for that URL.

Checksums are SHA-256 of the content-encoding-unwrapped bytes. Simhash runs
on text decoded with the charset declared in Content-Type. A weak validator
does not prove the stored bytes are unchanged.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field

from incremental_crawl_lab.config import Config
from incremental_crawl_lab.fingerprint import (
    format_simhash,
    hamming,
    parse_simhash,
    sha256_hex,
    simhash_text,
)


@dataclass
class Decision:
    kind: str
    charges_page: bool
    counts_observation: bool
    oracle_sync: bool
    updates_sync: bool
    status: str
    material_delta: int = 0
    byte_delta: int = 0
    cosmetic_delta: int = 0
    columns: dict = field(default_factory=dict)
    soft_error: bool = False
    distance: int | None = None
    reset_failures: bool = True
    byte_identity_known: bool | None = None
    charset_unknown: bool = False


def bind_soft_hashes(config: Config) -> None:
    """Treat the empty body and the fixture not-found page as non-updates."""
    from incremental_crawl_lab.corpus import NOT_FOUND

    encoded = NOT_FOUND.encode("utf-8")
    if not config.soft_sha256:
        config.soft_sha256 = (sha256_hex(encoded), sha256_hex(b""))
    if not config.soft_simhashes:
        config.soft_simhashes = (
            format_simhash(simhash_text(NOT_FOUND)),
            format_simhash(0),
        )


def split_content_type(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    parts = [part.strip() for part in value.split(";") if part.strip()]
    if not parts:
        return None, None
    media = parts[0].lower()
    charset = None
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"').strip("'").lower()
    return media, charset or None


def charset_supported(name: str | None) -> bool:
    if not name:
        return False
    try:
        codecs.lookup(name)
    except LookupError:
        return False
    return True


def split_etag(value: str | None) -> tuple[str | None, bool]:
    """Return ``(opaque, weak)``. The opaque form keeps its quotes."""
    if value is None:
        return None, False
    text = value.strip()
    if not text:
        return None, False
    weak = False
    if len(text) >= 2 and text[:2].upper() == "W/":
        weak = True
        text = text[2:].strip()
    return text or None, weak


def format_etag(opaque: str, weak: bool) -> str:
    body = opaque.strip()
    if len(body) >= 2 and body[:2].upper() == "W/":
        body = body[2:].strip()
    if weak:
        return "W/" + body
    return body


def parse_etag_list(value: str) -> list[str]:
    if value.strip() == "*":
        return ["*"]
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
            buf.append(char)
        elif char == "," and not quoted:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
        else:
            buf.append(char)
    piece = "".join(buf).strip()
    if piece:
        parts.append(piece)
    return parts


def etags_match(left: str | None, right: str | None) -> bool:
    """Weak comparison: W/\"v\" matches \"v\"."""
    if left is not None and left.strip() == "*":
        return right is not None
    left_opaque, _ = split_etag(left)
    right_opaque, _ = split_etag(right)
    return left_opaque is not None and left_opaque == right_opaque


def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def _is_soft(sha: str, simhash: str | None, config: Config) -> bool:
    if sha in config.soft_sha256:
        return True
    if simhash is None:
        return False
    return simhash in config.soft_simhashes or simhash == format_simhash(0)


def interpret(
    row: dict,
    status: int,
    headers: dict[str, str],
    body: bytes | None,
    config: Config,
) -> Decision:
    """Classify a terminal catalog response. ``body`` is already unwrapped."""
    headers = _lower_headers(headers)
    if status == 304:
        return _interpret_not_modified(row, headers, config)
    if status in (404, 410):
        return Decision(
            kind="gone",
            charges_page=True,
            counts_observation=False,
            oracle_sync=False,
            updates_sync=False,
            status="gone",
        )
    if status != 200:
        raise ValueError(f"interpret does not classify status {status}")
    if body is None:
        raise ValueError("a 200 response needs a body")
    return _interpret_ok(row, headers, body, config)


def _validator_columns(headers: dict[str, str]) -> tuple[dict, bool]:
    columns: dict = {}
    weak = False
    if "etag" in headers:
        opaque, weak = split_etag(headers.get("etag"))
        columns["etag"] = opaque
        columns["weak"] = 1 if weak else 0
    else:
        columns["etag"] = None
        columns["weak"] = 0
    if "last-modified" in headers:
        columns["last_modified"] = headers.get("last-modified")
    return columns, weak


def _interpret_not_modified(row: dict, headers: dict[str, str], config: Config) -> Decision:
    sent_weak = bool(row.get("weak")) and bool(row.get("etag"))
    columns: dict = {
        "variant_accept": config.accept,
        "variant_lang": config.accept_language,
    }
    if "etag" in headers:
        opaque, response_weak = split_etag(headers.get("etag"))
        columns["etag"] = opaque
        columns["weak"] = 1 if response_weak else 0
    else:
        response_weak = sent_weak
    if "last-modified" in headers:
        columns["last_modified"] = headers.get("last-modified")
    certifies = not sent_weak
    if certifies:
        identity = not response_weak
    else:
        identity = False
    return Decision(
        kind="validator_not_modified",
        charges_page=True,
        counts_observation=certifies,
        oracle_sync=certifies,
        updates_sync=True,
        status="live",
        byte_identity_known=identity,
        columns=columns,
    )


def _interpret_ok(row: dict, headers: dict[str, str], body: bytes, config: Config) -> Decision:
    media, charset = split_content_type(headers.get("content-type"))
    columns, weak = _validator_columns(headers)
    columns["media_type"] = media
    columns["sha256"] = sha256_hex(body)
    columns["body"] = body
    columns["variant_accept"] = config.accept
    columns["variant_lang"] = config.accept_language
    text: str | None = None
    if charset_supported(charset):
        try:
            text = body.decode(charset)
        except UnicodeDecodeError:
            # The declared charset does not describe these bytes. Keep the
            # checksum and skip simhash, the same as a missing charset.
            text = None
    unknown = text is None
    simhash: str | None
    if unknown:
        columns["charset"] = None
        columns["charset_unknown"] = 1
        columns["simhash"] = None
        simhash = None
    else:
        columns["charset"] = charset
        columns["charset_unknown"] = 0
        simhash = format_simhash(simhash_text(text))
        columns["simhash"] = simhash
    sha = columns["sha256"]
    identity = not weak
    if _is_soft(sha, simhash, config):
        previous = row.get("sha256")
        return Decision(
            kind="soft_error",
            charges_page=True,
            counts_observation=False,
            oracle_sync=True,
            updates_sync=True,
            status="live",
            material_delta=0,
            byte_delta=0 if previous in (None, sha) else 1,
            cosmetic_delta=0,
            columns=columns,
            soft_error=True,
            byte_identity_known=identity,
            charset_unknown=unknown,
        )
    previous_sha = row.get("sha256")
    previous_sim = row.get("simhash")
    distance: int | None = None
    if not previous_sha:
        kind = "baseline"
        material = byte = cosmetic = 0
    else:
        byte = 0 if previous_sha == sha else 1
        if byte == 0:
            kind = "byte_same"
            material = cosmetic = 0
        elif (
            unknown
            or not previous_sim
            or not simhash
            or not config.material_detection
            or config.simhash_k is None
        ):
            kind = "byte_change_unclassified"
            material = cosmetic = 0
        else:
            distance = hamming(parse_simhash(previous_sim), parse_simhash(simhash))
            if distance <= config.simhash_k:
                kind = "cosmetic_change"
                material = 0
                cosmetic = 1
            else:
                kind = "material_change"
                material = 1
                cosmetic = 0
    return Decision(
        kind=kind,
        charges_page=True,
        counts_observation=True,
        oracle_sync=True,
        updates_sync=True,
        status="live",
        material_delta=material,
        byte_delta=byte,
        cosmetic_delta=cosmetic,
        columns=columns,
        distance=distance,
        byte_identity_known=identity,
        charset_unknown=unknown,
    )
