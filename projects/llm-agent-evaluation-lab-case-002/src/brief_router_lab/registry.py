"""Typed tool registry: schemas, costs, roles, workspaces, capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import GOAL_KINDS


@dataclass(frozen=True)
class ToolSpec:
    name: str
    side_effect: str
    min_role: str
    allowed_workspaces: frozenset[str]
    token_cost: int
    arg_schema: dict[str, Any]
    lookup_kind: str | None = None
    lookup_arg: str | None = None


CATALOG_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query", "limit"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 80},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
    },
}

CATALOG_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["asset_id"],
    "properties": {
        "asset_id": {"type": "string", "minLength": 3, "maxLength": 24, "pattern": r"^AST-[0-9]+$"},
    },
}

KB_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 80},
    },
}

KB_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["note_id"],
    "properties": {
        "note_id": {"type": "string", "minLength": 3, "maxLength": 24, "pattern": r"^NOTE-[0-9]+$"},
    },
}

DRAFT_COMPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "source_ids", "notes"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 80},
        "source_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {"type": "string", "minLength": 3, "maxLength": 24, "pattern": r"^AST-[0-9]+$"},
        },
        "notes": {"type": "string", "minLength": 1, "maxLength": 240},
    },
}

DRAFT_CITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["draft_id", "note_id"],
    "properties": {
        "draft_id": {"type": "string", "minLength": 3, "maxLength": 40},
        "note_id": {"type": "string", "minLength": 3, "maxLength": 24, "pattern": r"^NOTE-[0-9]+$"},
    },
}

REVIEW_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["draft_id"],
    "properties": {
        "draft_id": {"type": "string", "minLength": 3, "maxLength": 40},
    },
}

PUBLISH_QUEUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["draft_id"],
    "properties": {
        "draft_id": {"type": "string", "minLength": 3, "maxLength": 40},
    },
}

CALENDAR_HOLD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["slot_id", "draft_id"],
    "properties": {
        "slot_id": {"type": "string", "minLength": 3, "maxLength": 24, "pattern": r"^SLOT-[A-Z0-9]+$"},
        "draft_id": {"type": "string", "minLength": 3, "maxLength": 40},
    },
}

REFUSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reason"],
    "properties": {
        "reason": {"type": "string", "minLength": 1, "maxLength": 160},
    },
}


TOOLS: dict[str, ToolSpec] = {
    "catalog.search": ToolSpec(
        name="catalog.search",
        side_effect="read",
        min_role="viewer",
        allowed_workspaces=frozenset({"public", "internal", "restricted"}),
        token_cost=20,
        arg_schema=CATALOG_SEARCH_SCHEMA,
    ),
    "catalog.get": ToolSpec(
        name="catalog.get",
        side_effect="read",
        min_role="viewer",
        allowed_workspaces=frozenset({"public", "internal", "restricted"}),
        token_cost=10,
        arg_schema=CATALOG_GET_SCHEMA,
        lookup_kind="asset",
        lookup_arg="asset_id",
    ),
    "kb.search": ToolSpec(
        name="kb.search",
        side_effect="read",
        min_role="viewer",
        allowed_workspaces=frozenset({"public", "internal", "restricted"}),
        token_cost=20,
        arg_schema=KB_SEARCH_SCHEMA,
    ),
    "kb.get": ToolSpec(
        name="kb.get",
        side_effect="read",
        min_role="viewer",
        allowed_workspaces=frozenset({"public", "internal", "restricted"}),
        token_cost=10,
        arg_schema=KB_GET_SCHEMA,
        lookup_kind="note",
        lookup_arg="note_id",
    ),
    "draft.compose": ToolSpec(
        name="draft.compose",
        side_effect="write",
        min_role="editor",
        allowed_workspaces=frozenset({"internal", "restricted"}),
        token_cost=80,
        arg_schema=DRAFT_COMPOSE_SCHEMA,
    ),
    "draft.cite": ToolSpec(
        name="draft.cite",
        side_effect="write",
        min_role="editor",
        allowed_workspaces=frozenset({"internal", "restricted"}),
        token_cost=15,
        arg_schema=DRAFT_CITE_SCHEMA,
        lookup_kind="note",
        lookup_arg="note_id",
    ),
    "review.submit": ToolSpec(
        name="review.submit",
        side_effect="write",
        min_role="editor",
        allowed_workspaces=frozenset({"internal", "restricted"}),
        token_cost=10,
        arg_schema=REVIEW_SUBMIT_SCHEMA,
    ),
    "publish.queue": ToolSpec(
        name="publish.queue",
        side_effect="sensitive",
        min_role="publisher",
        allowed_workspaces=frozenset({"internal"}),
        token_cost=25,
        arg_schema=PUBLISH_QUEUE_SCHEMA,
    ),
    "calendar.hold": ToolSpec(
        name="calendar.hold",
        side_effect="write",
        min_role="editor",
        allowed_workspaces=frozenset({"internal", "restricted"}),
        token_cost=10,
        arg_schema=CALENDAR_HOLD_SCHEMA,
    ),
    "refuse": ToolSpec(
        name="refuse",
        side_effect="none",
        min_role="viewer",
        allowed_workspaces=frozenset({"public", "internal", "restricted"}),
        token_cost=5,
        arg_schema=REFUSE_SCHEMA,
    ),
}


GOAL_TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "lookup": frozenset({"catalog.search", "catalog.get", "kb.search", "kb.get", "refuse"}),
    "research_brief": frozenset(
        {
            "catalog.search",
            "catalog.get",
            "kb.search",
            "kb.get",
            "draft.compose",
            "draft.cite",
            "review.submit",
            "refuse",
        }
    ),
    "publish": frozenset(
        {
            "catalog.get",
            "draft.compose",
            "review.submit",
            "publish.queue",
            "refuse",
        }
    ),
    "schedule": frozenset({"catalog.get", "draft.compose", "calendar.hold", "refuse"}),
    "refuse": frozenset({"refuse"}),
}

WRITE_SIDE_EFFECTS = frozenset({"write", "sensitive"})


def get_tool(name: str) -> ToolSpec | None:
    return TOOLS.get(name)


def planned_cost(tool_names: list[str]) -> int:
    total = 0
    for name in tool_names:
        spec = TOOLS.get(name)
        if spec is None:
            continue
        total += spec.token_cost
    return total


def allowed_tools(goal_kind: str) -> frozenset[str]:
    if goal_kind not in GOAL_KINDS:
        return frozenset()
    return GOAL_TOOL_ALLOWLIST[goal_kind]
