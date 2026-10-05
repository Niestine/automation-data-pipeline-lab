"""Canonical JSON and the allotment resource contract."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from .errors import MalformedIdempotencyKey, MissingIdempotencyKey, SchemaError

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PLOT = re.compile(r"^[A-Z0-9-]{1,16}$")
_NOTE = re.compile(r"^[a-z0-9_-]{1,32}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CALLER = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

RESOURCE_FIELDS = (
    "beds",
    "holder_label",
    "note",
    "plot_code",
    "resource_id",
    "sort_key",
    "source_version",
)

# Idempotency-Key retention published by this lab. After this the same key
# is a new execution. Page tokens use the separate AIP-158 three-day window.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
TOKEN_TTL_SECONDS = 3 * 24 * 60 * 60
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 1000
WEBHOOK_SKEW_SECONDS = 300
# Longer than the specification ladder, whose last attempt is at 75:35:05.
INBOX_RETENTION_SECONDS = 4 * 24 * 60 * 60
SPEC_LADDER_LAST_SECONDS = 75 * 3600 + 35 * 60 + 5

# 32-byte synthetic HMAC key for the in-process receiver. Not a live secret.
LAB_WEBHOOK_SECRET = b"plot-allotment-lab-hmac-key-0001"
LAB_NAMESPACE = uuid.UUID("6f0c2e1a-7b14-5d0e-9c3a-11a0b7e4d2f8")


def loads_strict(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaError("body is not utf-8") from exc

    def reject_constant(_name: str) -> None:
        raise SchemaError("non-finite number")

    try:
        return json.loads(text, parse_constant=reject_constant)
    except SchemaError:
        raise
    except json.JSONDecodeError as exc:
        raise SchemaError("body is not json") from exc


def canonical_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def fingerprint(parent: str, body: dict[str, Any]) -> str:
    """SHA-256 of method, path, and canonical body. Headers are not included."""
    material = f"POST\n/v1/{parent}/resources:upsert\n{canonical_dumps(body)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def param_fingerprint(parent: str, filter_raw: str, order: str) -> str:
    material = f"{parent}\n{filter_raw}\n{order}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_idempotency_key(header: str | None) -> str:
    """Require a quoted lowercase UUID before any idempotency-table read."""
    if header is None or header.strip() == "":
        raise MissingIdempotencyKey("Idempotency-Key is required")
    value = header.strip()
    if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
        raise MalformedIdempotencyKey("Idempotency-Key must be a quoted string")
    inner = value[1:-1]
    if "\\" in inner or not _UUID.match(inner):
        raise MalformedIdempotencyKey("Idempotency-Key must be a lowercase UUID")
    return inner


def format_idempotency_key(key: str) -> str:
    parse_idempotency_key(f'"{key}"')
    return f'"{key}"'


def page_replay_key(
    caller: str, snapshot_id: str, resource_id: str, source_version: int
) -> str:
    """Stable UUID derived from the natural replay identity."""
    name = f"{caller}|{snapshot_id}|{resource_id}|{source_version}"
    return str(uuid.uuid5(LAB_NAMESPACE, name))


def require_parent(value: Any) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise SchemaError("parent is invalid")
    return value


def parse_caller(header: str | None) -> str | None:
    if not header or not header.startswith("Bearer "):
        return None
    label = header[7:]
    if not _CALLER.match(label):
        return None
    return label


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{name} must be an integer")
    return value


def resource_body(value: Any, *, allow_request_id: bool) -> dict[str, Any]:
    """Validate an upsert body. request_id is accepted and then removed."""
    if not isinstance(value, dict):
        raise SchemaError("body must be an object")
    allowed = set(RESOURCE_FIELDS)
    if allow_request_id:
        allowed.add("request_id")
    unknown = set(value) - allowed
    if unknown:
        raise SchemaError("unknown field: " + ",".join(sorted(unknown)))
    missing = [name for name in RESOURCE_FIELDS if name not in value]
    if missing:
        raise SchemaError("missing field: " + ",".join(missing))
    if "request_id" in value:
        request_id = value["request_id"]
        if not isinstance(request_id, str) or not _UUID.match(request_id):
            raise SchemaError("request_id must be a lowercase UUID")
    resource_id = value["resource_id"]
    if not isinstance(resource_id, str) or not _ID.match(resource_id):
        raise SchemaError("resource_id is invalid")
    holder = value["holder_label"]
    if not isinstance(holder, str) or not _LABEL.match(holder):
        raise SchemaError("holder_label is invalid")
    plot = value["plot_code"]
    if not isinstance(plot, str) or not _PLOT.match(plot):
        raise SchemaError("plot_code is invalid")
    note = value["note"]
    if not isinstance(note, str) or not _NOTE.match(note):
        raise SchemaError("note is invalid")
    beds = _require_int(value["beds"], "beds")
    if beds < 0:
        raise SchemaError("beds must be >= 0")
    sort_key = _require_int(value["sort_key"], "sort_key")
    source_version = _require_int(value["source_version"], "source_version")
    if source_version < 1:
        raise SchemaError("source_version must be >= 1")
    return {
        "beds": beds,
        "holder_label": holder,
        "note": note,
        "plot_code": plot,
        "resource_id": resource_id,
        "sort_key": sort_key,
        "source_version": source_version,
    }


def parse_filter(raw: str | None) -> tuple[str, str | None]:
    """Return the canonical filter string and the note value it selects."""
    if raw is None or raw == "":
        return "", None
    if not raw.startswith("note=") or not _NOTE.match(raw[5:]):
        raise SchemaError("filter must be empty or note=<value>")
    return raw, raw[5:]


def parse_order(raw: str | None) -> str:
    if raw is None or raw == "" or raw == "sort_key":
        return "sort_key"
    raise SchemaError("order must be sort_key")


def coerce_page_size(raw: str | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_PAGE_SIZE
    if not re.fullmatch(r"-?\d+", raw):
        raise SchemaError("page_size must be an integer")
    value = int(raw)
    if value < 0:
        raise SchemaError("page_size must be >= 0")
    if value == 0:
        return DEFAULT_PAGE_SIZE
    if value > MAX_PAGE_SIZE:
        return MAX_PAGE_SIZE
    return value
