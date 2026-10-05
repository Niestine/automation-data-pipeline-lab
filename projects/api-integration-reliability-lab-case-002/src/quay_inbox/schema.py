"""Schemas selected by the event's own api_version, not the process default."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import SchemaError
from .horizons import API_VERSIONS, KNOWN_TYPES, SELECTION_REQUIRED

_FIELDS = {
    "2024-09-01": ("id", "status", "berth"),
    "2026-01-01": ("id", "status", "berth", "yard_code"),
}
_STATUSES = frozenset({"granted", "held", "opened", "settled"})


@dataclass(frozen=True)
class Notice:
    event_id: str
    event_type: str
    api_version: str
    created: int | None
    object_id: str
    snapshot: dict
    thin: bool
    profile: str

    def to_json(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "api_version": self.api_version,
            "created": self.created,
            "object_id": self.object_id,
            "snapshot": self.snapshot,
            "thin": self.thin,
            "profile": self.profile,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "Notice":
        return cls(
            event_id=payload["event_id"],
            event_type=payload["event_type"],
            api_version=payload["api_version"],
            created=payload["created"],
            object_id=payload["object_id"],
            snapshot=dict(payload["snapshot"]),
            thin=bool(payload["thin"]),
            profile=payload["profile"],
        )


def loads(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError("malformed json") from exc
    if not isinstance(payload, dict):
        raise SchemaError("payload must be an object")
    return payload


def _check_type(event_type: object) -> str:
    if not isinstance(event_type, str) or event_type not in KNOWN_TYPES:
        raise SchemaError("unknown event type")
    return event_type


def _check_version(api_version: object) -> str:
    if not isinstance(api_version, str) or api_version not in API_VERSIONS:
        raise SchemaError("unknown api_version")
    return api_version


def business_view(snapshot: dict, api_version: str) -> dict:
    fields = _FIELDS[api_version]
    view = {key: snapshot[key] for key in fields if key in snapshot}
    return view


def validate_snapshot(snapshot: dict, api_version: str, *, thin: bool) -> dict:
    version = _check_version(api_version)
    if not isinstance(snapshot, dict):
        raise SchemaError("snapshot must be an object")
    allowed_extra = {"object"}
    if thin:
        keys = set(snapshot) - allowed_extra
        if keys != {"id"}:
            raise SchemaError("thin snapshot carries only the object id")
        if snapshot.get("object", "release") != "release":
            raise SchemaError("object discriminator")
        if not isinstance(snapshot["id"], str) or not snapshot["id"]:
            raise SchemaError("object id")
        return {"id": snapshot["id"]}
    fields = _FIELDS[version]
    keys = set(snapshot) - allowed_extra
    if keys != set(fields):
        raise SchemaError("snapshot fields do not match api_version")
    if snapshot.get("object", "release") != "release":
        raise SchemaError("object discriminator")
    if not isinstance(snapshot["id"], str) or not snapshot["id"]:
        raise SchemaError("object id")
    if snapshot["status"] not in _STATUSES:
        raise SchemaError("status")
    if not isinstance(snapshot["berth"], str) or not snapshot["berth"]:
        raise SchemaError("berth")
    if "yard_code" in fields:
        if not isinstance(snapshot["yard_code"], str) or not snapshot["yard_code"]:
            raise SchemaError("yard_code")
    return business_view(snapshot, version)


def notice_from_standard(event_id: str, payload: dict) -> Notice:
    if set(payload) != {"type", "api_version", "data"} and set(payload) != {
        "type",
        "api_version",
        "data",
        "created",
    }:
        raise SchemaError("standard envelope")
    event_type = _check_type(payload.get("type"))
    api_version = _check_version(payload.get("api_version"))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SchemaError("data")
    thin = "status" not in data
    snapshot = validate_snapshot(data, api_version, thin=thin)
    created = payload.get("created")
    if created is not None and (not isinstance(created, int) or isinstance(created, bool)):
        raise SchemaError("created")
    return Notice(event_id, event_type, api_version, created, snapshot["id"], snapshot, thin, "standard")


def notice_from_stripe(payload: dict) -> Notice:
    required = {"id", "object", "api_version", "created", "type", "data"}
    if set(payload) != required:
        raise SchemaError("stripe envelope")
    if payload["object"] != "event":
        raise SchemaError("event object")
    if not isinstance(payload["id"], str) or not payload["id"]:
        raise SchemaError("event id")
    if not isinstance(payload["created"], int) or isinstance(payload["created"], bool):
        raise SchemaError("created")
    event_type = _check_type(payload["type"])
    api_version = _check_version(payload["api_version"])
    data = payload["data"]
    if not isinstance(data, dict) or set(data) != {"object"}:
        raise SchemaError("data")
    obj = data["object"]
    if not isinstance(obj, dict):
        raise SchemaError("data.object")
    thin = "status" not in obj
    snapshot = validate_snapshot(obj, api_version, thin=thin)
    return Notice(
        payload["id"],
        event_type,
        api_version,
        payload["created"],
        snapshot["id"],
        snapshot,
        thin,
        "stripe",
    )


def parse_coverage_notice(payload: dict) -> Notice:
    """Coverage signs the body. The inbox id is the id inside that body."""

    if "id" not in payload:
        raise SchemaError("event id")
    event_id = payload["id"]
    if not isinstance(event_id, str) or not event_id:
        raise SchemaError("event id")
    rest = {key: value for key, value in payload.items() if key != "id"}
    notice = notice_from_standard(event_id, rest)
    return Notice(
        event_id,
        notice.event_type,
        notice.api_version,
        notice.created,
        notice.object_id,
        notice.snapshot,
        notice.thin,
        "coverage",
    )


def filter_subscription(events: list[dict], subscribed: set[str] | None) -> list[dict]:
    """An all-events subscription does not receive selection-required types."""

    if subscribed is None:
        return [event for event in events if event.get("type") not in SELECTION_REQUIRED]
    return [event for event in events if event.get("type") in subscribed]
