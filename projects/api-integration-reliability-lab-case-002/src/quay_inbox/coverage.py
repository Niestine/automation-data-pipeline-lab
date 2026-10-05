"""One pinned HTTP message-signature profile, not an RFC 9421 stack.

Covered components, in order: ``@method``, ``@authority``, ``@path``,
``content-digest``, then the ``@signature-params`` line. The only algorithm
is ``hmac-sha256``. ``created``, ``expires``, and ``nonce`` are rejected
because this archive's RFC extract does not include that replay prose.

Content-Digest is SHA-256 over the raw content octets, written as a
Structured Field byte sequence ``sha-256=:base64=:``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from .errors import AuthError

PROFILE_COMPONENTS = ("@method", "@authority", "@path", "content-digest")
PROFILE_LABEL = "quay"
PROFILE_ALG = "hmac-sha256"


def content_digest(body: bytes) -> str:
    digest = hashlib.sha256(body).digest()
    encoded = base64.b64encode(digest).decode("ascii")
    return f"sha-256=:{encoded}:"


def signature_base(
    *,
    method: str,
    authority: str,
    path: str,
    digest_header: str,
    params_rest: str,
) -> bytes:
    lines = (
        f'"@method": {method}',
        f'"@authority": {authority}',
        f'"@path": {path}',
        f'"content-digest": {digest_header}',
        f'"@signature-params": {params_rest}',
    )
    return ("\n".join(lines) + "\n").encode("ascii")


def _params_rest(keyid: str) -> str:
    inner = " ".join(f'"{name}"' for name in PROFILE_COMPONENTS)
    return f'({inner});alg="{PROFILE_ALG}";keyid="{keyid}"'


def sign_coverage(
    secret: bytes,
    *,
    method: str,
    authority: str,
    path: str,
    body: bytes,
    keyid: str = "quay-coverage",
) -> dict[str, str]:
    authority = authority.lower()
    digest_header = content_digest(body)
    rest = _params_rest(keyid)
    base = signature_base(
        method=method,
        authority=authority,
        path=path,
        digest_header=digest_header,
        params_rest=rest,
    )
    mac = hmac.new(secret, base, hashlib.sha256).digest()
    encoded = base64.b64encode(mac).decode("ascii")
    return {
        "content-digest": digest_header,
        "signature-input": f"{PROFILE_LABEL}={rest}",
        "signature": f"{PROFILE_LABEL}=:{encoded}:",
    }


def _split_components(rest: str) -> tuple[list[str], str]:
    if not rest.startswith("("):
        raise AuthError("coverage")
    end = rest.find(")")
    if end < 0:
        raise AuthError("coverage")
    inner = rest[1:end]
    components: list[str] = []
    if inner:
        for part in inner.split(" "):
            if len(part) < 2 or not part.startswith('"') or not part.endswith('"'):
                raise AuthError("coverage")
            components.append(part[1:-1])
    return components, rest


def _param_map(rest: str) -> dict[str, str]:
    after = rest.split(")", 1)[1]
    params: dict[str, str] = {}
    for piece in after.split(";"):
        if not piece:
            continue
        if "=" not in piece:
            raise AuthError("coverage")
        name, value = piece.split("=", 1)
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if name in params:
            raise AuthError("coverage")
        params[name] = value
    return params


def verify_coverage(
    *,
    method: str,
    authority: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
    secrets: list[bytes] | tuple[bytes, ...],
    keyid: str,
) -> None:
    """Fail closed unless the covered set is exactly the pinned profile."""

    raw_input = headers.get("signature-input", "")
    raw_signature = headers.get("signature", "")
    digest_header = headers.get("content-digest", "")
    if not raw_input or not raw_signature or not digest_header:
        raise AuthError("missing")
    if headers.get("digest") or headers.get("repr-digest"):
        raise AuthError("coverage")
    label, separator, rest = raw_input.partition("=")
    if separator != "=" or label != PROFILE_LABEL or not rest:
        raise AuthError("coverage")
    components, params_rest = _split_components(rest)
    if tuple(components) != PROFILE_COMPONENTS:
        raise AuthError("coverage")
    params = _param_map(rest)
    if params.get("alg") != PROFILE_ALG:
        raise AuthError("coverage")
    if any(name in params for name in ("created", "expires", "nonce")):
        raise AuthError("coverage")
    if params.get("keyid") != keyid:
        raise AuthError("route")
    # One sha-256 byte sequence over the raw octets. A second algorithm,
    # hex, or a digest of a reserialized document does not match.
    if digest_header != content_digest(body):
        raise AuthError("digest")
    prefix = PROFILE_LABEL + "=:"
    if not raw_signature.startswith(prefix) or not raw_signature.endswith(":"):
        raise AuthError("coverage")
    encoded = raw_signature[len(prefix) : -1]
    try:
        presented = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise AuthError("coverage") from exc
    authority = authority.lower()
    if "?" in path:
        raise AuthError("coverage")
    base = signature_base(
        method=method,
        authority=authority,
        path=path,
        digest_header=digest_header,
        params_rest=params_rest,
    )
    for secret in secrets:
        expected = hmac.new(secret, base, hashlib.sha256).digest()
        if len(presented) == len(expected) and hmac.compare_digest(presented, expected):
            return
    raise AuthError("mac")
