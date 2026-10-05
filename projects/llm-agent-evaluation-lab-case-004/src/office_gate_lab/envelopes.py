"""Action envelopes admitted against the strict dialect."""

from __future__ import annotations

from typing import Any

from .schema_dialect import admit

NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}

SLOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source", "value", "from_step", "field"],
    "properties": {
        "source": {"type": "string", "enum": ["literal", "extract"]},
        "value": NULLABLE_STRING,
        "from_step": NULLABLE_STRING,
        "field": NULLABLE_STRING,
    },
}

NULLABLE_SLOT: dict[str, Any] = {"anyOf": [SLOT_SCHEMA, {"type": "null"}]}

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["step_id", "tool", "arguments", "predicate"],
    "properties": {
        "step_id": {"type": "string", "pattern": "^s[1-9][0-9]*$"},
        "tool": {
            "type": "string",
            "enum": ["list_messages", "read_document", "ask_user", "send_document"],
        },
        "arguments": {
            "type": "object",
            "additionalProperties": False,
            "required": ["document_id", "recipient", "question_id"],
            "properties": {
                "document_id": NULLABLE_SLOT,
                "recipient": NULLABLE_SLOT,
                "question_id": NULLABLE_SLOT,
            },
        },
        "predicate": NULLABLE_SLOT,
    },
}

PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "steps"],
    "properties": {
        "kind": {"type": "string", "const": "plan"},
        "steps": {
            "type": "array",
            "minItems": 0,
            "maxItems": 8,
            "items": STEP_SCHEMA,
        },
    },
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "fields"],
    "properties": {
        "kind": {"type": "string", "const": "extract"},
        "fields": {
            "type": "object",
            "additionalProperties": False,
            "required": ["document_id", "named_recipient", "title", "urgent", "answer"],
            "properties": {
                "document_id": NULLABLE_STRING,
                "named_recipient": NULLABLE_STRING,
                "title": NULLABLE_STRING,
                "urgent": NULLABLE_STRING,
                "answer": NULLABLE_STRING,
            },
        },
    },
}

REFUSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "reason"],
    "properties": {
        "kind": {"type": "string", "const": "refusal"},
        "reason": {"type": "string"},
    },
}

CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claim_id", "value", "evidence_field"],
    "properties": {
        "claim_id": {"type": "string", "pattern": "^c[1-9][0-9]*$"},
        "value": {"type": "string"},
        "evidence_field": NULLABLE_STRING,
    },
}

FINISH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "claims"],
    "properties": {
        "kind": {"type": "string", "const": "finish"},
        "claims": {
            "type": "array",
            "minItems": 0,
            "maxItems": 8,
            "items": CLAIM_SCHEMA,
        },
    },
}

SEND_ARGUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document_id", "recipient"],
    "properties": {
        "document_id": {"type": "string", "pattern": "^doc-[a-z0-9-]+$"},
        "recipient": {"type": "string", "format": "email"},
    },
}

MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "subject", "sender", "body"],
    "properties": {
        "id": {"type": "string"},
        "subject": {"type": "string"},
        "sender": {"type": "string"},
        "body": {"type": "string"},
    },
}

READ_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document_id", "title", "readers", "body", "named_recipient", "urgent"],
    "properties": {
        "document_id": {"type": "string"},
        "title": {"type": "string"},
        "readers": {
            "type": "array",
            "minItems": 0,
            "maxItems": 8,
            "items": {"type": "string"},
        },
        "body": {"type": "string"},
        "named_recipient": NULLABLE_STRING,
        "urgent": NULLABLE_STRING,
    },
}

LIST_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["messages"],
    "properties": {
        "messages": {
            "type": "array",
            "minItems": 0,
            "maxItems": 8,
            "items": MESSAGE_SCHEMA,
        },
    },
}

SEND_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document_id", "recipient", "outbox_id", "status"],
    "properties": {
        "document_id": {"type": "string"},
        "recipient": {"type": "string"},
        "outbox_id": {"type": "string"},
        "status": {"type": "string", "const": "sent"},
    },
}

ASK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["question_id", "answer"],
    "properties": {
        "question_id": {"type": "string"},
        "answer": NULLABLE_STRING,
    },
}

READ_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document_id"],
    "properties": {
        "document_id": {"type": "string", "pattern": "^doc-[a-z0-9-]+$"},
    },
}

LIST_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {},
}

ASK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["question_id"],
    "properties": {
        "question_id": {"type": "string", "pattern": "^[a-z0-9_]+$"},
    },
}

OUTPUT_SCHEMAS = {
    "list_messages": LIST_OUTPUT_SCHEMA,
    "read_document": READ_OUTPUT_SCHEMA,
    "ask_user": ASK_OUTPUT_SCHEMA,
    "send_document": SEND_OUTPUT_SCHEMA,
}

INPUT_SCHEMAS = {
    "list_messages": LIST_INPUT_SCHEMA,
    "read_document": READ_INPUT_SCHEMA,
    "ask_user": ASK_INPUT_SCHEMA,
    "send_document": SEND_ARGUMENT_SCHEMA,
}

for _schema in (
    PLAN_SCHEMA,
    EXTRACT_SCHEMA,
    REFUSAL_SCHEMA,
    FINISH_SCHEMA,
    SEND_ARGUMENT_SCHEMA,
    READ_OUTPUT_SCHEMA,
    LIST_OUTPUT_SCHEMA,
    SEND_OUTPUT_SCHEMA,
    ASK_OUTPUT_SCHEMA,
    READ_INPUT_SCHEMA,
    LIST_INPUT_SCHEMA,
    ASK_INPUT_SCHEMA,
):
    admit(_schema)
