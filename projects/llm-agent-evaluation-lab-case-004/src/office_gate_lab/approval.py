"""Elicitation forms, resume validation, and a JSONL checkpoint.

Decisions follow the MCP elicitation result: accept, decline, or cancel.
content is accepted only with accept. Forms that ask for a secret are
rejected before a prompt is recorded. The lab validates the resume object
locally; a structural pass is not the capability policy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .codec import canonical
from .redact import is_secret_key

SECRET_WORDS = frozenset({"password", "passwords", "token", "tokens", "secret", "secrets"})
_WORD = re.compile(r"[a-z0-9_]+")

DEFAULT_ELICITATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recipient": {
            "type": "string",
            "description": "Replacement recipient address",
        },
        "confirm": {
            "type": "boolean",
            "description": "Confirm this send",
        },
    },
    "required": ["recipient", "confirm"],
}


def _description_asks_for_secret(description: str) -> bool:
    words = set(_WORD.findall(description.lower()))
    return bool(words & SECRET_WORDS)


def validate_elicitation_schema(schema: dict[str, Any]) -> str | None:
    """Return an error code, or None when the form may be shown."""
    if schema.get("type") != "object":
        return "elicitation_not_object"
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return "elicitation_properties"
    allowed_types = {"string", "integer", "boolean"}
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            return "elicitation_not_primitive"
        if is_secret_key(str(name)) or _description_asks_for_secret(str(prop.get("description", ""))):
            return "elicitation_secret"
        if "properties" in prop:
            return "elicitation_nested"
        if prop.get("type") not in allowed_types:
            return "elicitation_not_primitive"
    return None


def validate_resume(decision: Any, schema: dict[str, Any]) -> str | None:
    """Return an error code, or None when the decision can be applied."""
    if not isinstance(decision, dict) or "action" not in decision:
        return "missing_action"
    action = decision.get("action")
    if action not in {"accept", "decline", "cancel"}:
        return "bad_action"
    content = decision.get("content", None)
    has_content = content is not None
    if action != "accept" and has_content:
        return "content_on_" + str(action)
    if action == "accept" and not isinstance(content, dict):
        return "accept_without_content"
    if action != "accept":
        return None
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    for key in required:
        if key not in content:
            return "content_missing_" + str(key)
    for key, value in content.items():
        if key not in properties:
            return "content_additional"
        expected = properties[key].get("type")
        if expected == "string" and not isinstance(value, str):
            return "content_type"
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return "content_type"
        if expected == "boolean" and not isinstance(value, bool):
            return "content_type"
    # A form with a confirm box is an approval only when the box is ticked.
    if properties.get("confirm", {}).get("type") == "boolean" and content.get("confirm") is not True:
        return "accept_not_confirmed"
    return None


class CheckpointStore:
    """Append-only JSONL keyed by thread id. Invalid resumes write nothing.

    With a path, latest() reads the file, so a resume sees what was persisted
    rather than an in-memory copy. World state and bindings stay in the runner
    session; the checkpoint is the durable pause record, not a full snapshot.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.lines: list[str] = []

    def append(self, record: dict[str, Any]) -> None:
        line = canonical(record) + "\n"
        self.lines.append(line)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _stored_lines(self) -> list[str]:
        if self.path is None:
            return list(self.lines)
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()

    def latest(self, thread_id: str) -> dict[str, Any] | None:
        found = None
        for line in self._stored_lines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("thread_id") == thread_id:
                found = record
        return found
