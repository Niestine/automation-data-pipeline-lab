"""Episodes, edges, and the FACT-line parser.

Graph writes later accept only the closed edge fields. A model-authored query
string is not a field on this record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from fieldlog.support import count_tokens, parse_instant, resolve_time_expression

ABSENT_CONTEXT = {"", "none", "null"}


def project_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def schema_dir() -> Path:
    return project_dir() / "schemas"


def load_schemas() -> dict[str, dict]:
    loaded = {}
    for path in sorted(schema_dir().glob("*.json")):
        loaded[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    if not loaded:
        raise FileNotFoundError(f"no schemas in {schema_dir()}")
    return loaded


@dataclass
class Episode:
    episode_id: str
    session_id: str
    actor: str
    kind: str
    text: str
    reference_time: str
    token_count: int
    turn_index: int
    user_id: str = "crew"
    user_scope: bool = False
    declared_facts: list = field(default_factory=list)

    def public_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "session_id": self.session_id,
            "actor": self.actor,
            "kind": self.kind,
            "text": self.text,
            "reference_time": self.reference_time,
            "token_count": self.token_count,
            "turn_index": self.turn_index,
            "user_id": self.user_id,
            "user_scope": self.user_scope,
        }


@dataclass
class Edge:
    edge_id: str
    session_id: str
    episode_id: str
    subject: str
    predicate: str
    object: str
    serial: int | None
    t_valid: str | None
    t_invalid: str | None
    t_created: str
    t_expired: str | None
    source_label: str
    status: str
    span: str
    context: str | None = None

    def public_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "session_id": self.session_id,
            "episode_id": self.episode_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "serial": self.serial,
            "t_valid": self.t_valid,
            "t_invalid": self.t_invalid,
            "t_created": self.t_created,
            "t_expired": self.t_expired,
            "source_label": self.source_label,
            "status": self.status,
            "span": self.span,
            "context": self.context,
        }

    def order_key(self) -> tuple:
        serial = self.serial if self.serial is not None else -1
        return (serial, self.t_valid or "", self.edge_id)


def make_edge_id(session_id: str, episode_id: str, subject: str, predicate: str, serial: int | None, obj: str) -> str:
    raw = f"{session_id}|{episode_id}|{subject}|{predicate}|{serial}|{obj}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"edge-{digest}"


def parse_fact_lines(text: str, reference_time: str) -> list[dict]:
    """Pull FACT rows out of a turn. INSTR lines are data and are ignored here."""

    reference = parse_instant(reference_time)
    facts = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("FACT "):
            continue
        parts = [part.strip() for part in line[5:].split("|")]
        if len(parts) < 3:
            continue
        subject, predicate, obj = parts[0], parts[1], parts[2]
        meta: dict[str, str] = {}
        for extra in parts[3:]:
            if "=" not in extra:
                continue
            key, value = extra.split("=", 1)
            meta[key.strip()] = value.strip()
        serial = int(meta["serial"]) if "serial" in meta and meta["serial"] != "" else None
        if "valid" in meta:
            t_valid = resolve_time_expression(meta["valid"], reference)
        else:
            t_valid = reference
        context = meta.get("context")
        if context in ABSENT_CONTEXT:
            context = None
        facts.append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "serial": serial,
                "t_valid": t_valid,
                "source_label": meta.get("source", "unspecified"),
                "context": context,
                "close": meta.get("close", "supersede"),
                "span": line,
            }
        )
    return facts


def normalize_explicit_fact(payload: dict, reference_time: str, text: str) -> dict:
    """Side-channel fact whose citation span must already occur in the episode text."""

    reference = parse_instant(reference_time)
    span = payload.get("span") or ""
    if not span or span not in text:
        raise ValueError("explicit fact span is not a substring of the episode")
    valid_expr = payload.get("valid")
    if valid_expr:
        t_valid = resolve_time_expression(str(valid_expr), reference)
    else:
        t_valid = reference
    context = payload.get("context")
    if context in ABSENT_CONTEXT:
        context = None
    serial = payload.get("serial")
    if serial is not None:
        serial = int(serial)
    return {
        "subject": str(payload["subject"]),
        "predicate": str(payload["predicate"]),
        "object": str(payload["object"]),
        "serial": serial,
        "t_valid": t_valid,
        "source_label": str(payload.get("source", payload.get("source_label", "unspecified"))),
        "context": context,
        "close": str(payload.get("close", "supersede")),
        "span": span,
    }


def episode_token_count(text: str) -> int:
    tokens = count_tokens(text)
    return tokens if tokens > 0 else 1
