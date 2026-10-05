"""Canonical shift-slip payloads and the CSV a downstream lane tool reads."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re

from shiftlease.errors import ValidationError

_DESK = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_SKU = re.compile(r"^[A-Z0-9]{2,16}$")
_BIN = re.compile(r"^[A-Z][0-9]{2}$")
_SLOT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SCHEDULE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_ROW_FIELDS = {"sku", "qty", "bin"}


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(payload: dict) -> str:
    """SHA-256 of the canonical JSON body. Published id: sha256-canonical-json-v1."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest


def validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")
    extra = set(payload) - {"desk", "rows"}
    if extra:
        raise ValidationError("payload has unknown fields: " + ", ".join(sorted(extra)))
    desk = payload.get("desk")
    rows = payload.get("rows")
    if not isinstance(desk, str) or _DESK.fullmatch(desk) is None:
        raise ValidationError("desk must match ^[a-z][a-z0-9-]{0,31}$")
    if not isinstance(rows, list) or not rows or len(rows) > 50:
        raise ValidationError("rows must be a list of 1 to 50 objects")
    normalized_rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
            raise ValidationError(f"rows[{index}] must contain sku, qty, and bin")
        sku = row["sku"]
        qty = row["qty"]
        bin_code = row["bin"]
        if not isinstance(sku, str) or _SKU.fullmatch(sku) is None:
            raise ValidationError(f"rows[{index}].sku is invalid")
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 10000:
            raise ValidationError(f"rows[{index}].qty must be an integer from 1 to 10000")
        if not isinstance(bin_code, str) or _BIN.fullmatch(bin_code) is None:
            raise ValidationError(f"rows[{index}].bin is invalid")
        normalized_rows.append({"sku": sku, "qty": qty, "bin": bin_code})
    return {"desk": desk, "rows": normalized_rows}


def validate_schedule(schedule_id: str, slot_start: str) -> tuple[str, str]:
    if not isinstance(schedule_id, str) or _SCHEDULE.fullmatch(schedule_id) is None:
        raise ValidationError("schedule_id must match ^[a-z][a-z0-9-]{0,31}$")
    if not isinstance(slot_start, str) or _SLOT.fullmatch(slot_start) is None:
        raise ValidationError("slot_start must be YYYY-MM-DDTHH:MM:SSZ")
    return schedule_id, slot_start


def schedule_key(schedule_id: str, slot_start: str) -> str:
    schedule_id, slot_start = validate_schedule(schedule_id, slot_start)
    return f"sched:{schedule_id}:{slot_start}"


def render_csv(idempotency_key: str, payload: dict) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["idempotency_key", "desk", "sku", "qty", "bin"])
    for row in payload["rows"]:
        writer.writerow([idempotency_key, payload["desk"], row["sku"], row["qty"], row["bin"]])
    return buffer.getvalue().encode("utf-8")
