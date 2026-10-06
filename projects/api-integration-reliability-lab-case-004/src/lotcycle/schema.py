"""Request and document checks. Fingerprints cover the raw body, not Authorization."""

from __future__ import annotations

import hashlib
import json
import re

from lotcycle.errors import SchemaError

_SKU = re.compile(r"[a-z0-9-]{1,32}")
_ENTRY_ID = re.compile(r"[a-z]{2}-\d{3}")
_SCOPE_TOKEN = re.compile(r"[A-Za-z0-9._-]+")
_KEY_TOKEN = re.compile(r"[A-Za-z0-9._~-]{1,128}")


def canonical_json(document: dict) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def fingerprint(method: str, path: str, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(method.encode("utf-8"))
    digest.update(b"\n")
    digest.update(path.encode("utf-8"))
    digest.update(b"\n")
    digest.update(body)
    return digest.hexdigest()


def problem(status: int, title: str, detail: str) -> bytes:
    return canonical_json(
        {
            "detail": detail,
            "status": status,
            "title": title,
            "type": "about:blank",
        }
    )


def parse_idempotency_key(header: str | None) -> str:
    """RFC 8941 string: a quoted token. Missing and malformed are distinct."""
    if header is None or header == "":
        raise SchemaError("missing")
    text = header.strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        raise SchemaError("unquoted")
    inner = text[1:-1]
    if "\\" in inner or not _KEY_TOKEN.fullmatch(inner):
        raise SchemaError("malformed")
    return inner


def parse_scope(value: str | None) -> tuple[str, ...] | None:
    """None means the parameter was omitted. An empty tuple is malformed."""
    if value is None:
        return None
    parts = value.split()
    if not parts or any(_SCOPE_TOKEN.fullmatch(part) is None for part in parts):
        return ()
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return tuple(seen)


def scope_allows(granted: str, required: str) -> bool:
    return required in set(granted.split())


def is_subset(requested: tuple[str, ...], granted: str) -> bool:
    return set(requested).issubset(set(granted.split()))


def validate_order(document: object) -> dict:
    if not isinstance(document, dict):
        raise SchemaError("order type")
    if set(document) != {"qty", "sku"}:
        raise SchemaError("order fields")
    sku = document["sku"]
    qty = document["qty"]
    if not isinstance(sku, str) or _SKU.fullmatch(sku) is None:
        raise SchemaError("sku")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 1000:
        raise SchemaError("qty")
    return {"qty": qty, "sku": sku}


def validate_entry(document: object) -> dict:
    if not isinstance(document, dict):
        raise SchemaError("entry type")
    required = {"celsius", "id", "lot", "updated"}
    if not required.issubset(document):
        raise SchemaError("entry fields")
    entry_id = document["id"]
    lot = document["lot"]
    updated = document["updated"]
    celsius = document["celsius"]
    if not isinstance(entry_id, str) or _ENTRY_ID.fullmatch(entry_id) is None:
        raise SchemaError("entry id")
    if not isinstance(lot, str) or not lot:
        raise SchemaError("lot")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        raise SchemaError("updated")
    if isinstance(celsius, bool) or not isinstance(celsius, (int, float)):
        raise SchemaError("celsius")
    return {
        "celsius": celsius,
        "id": entry_id,
        "lot": lot,
        "updated": updated,
    }


def validate_webhook_event(document: object) -> dict:
    if not isinstance(document, dict):
        raise SchemaError("event type")
    if set(document) != {"occurred_at", "resource_id", "type"}:
        raise SchemaError("event fields")
    event_type = document["type"]
    resource_id = document["resource_id"]
    occurred_at = document["occurred_at"]
    if not isinstance(event_type, str) or "." not in event_type or len(event_type) > 64:
        raise SchemaError("event type value")
    if not isinstance(resource_id, str) or not resource_id or len(resource_id) > 64:
        raise SchemaError("resource id")
    if isinstance(occurred_at, bool) or not isinstance(occurred_at, (int, float)):
        raise SchemaError("occurred_at")
    return {
        "occurred_at": occurred_at,
        "resource_id": resource_id,
        "type": event_type,
    }
