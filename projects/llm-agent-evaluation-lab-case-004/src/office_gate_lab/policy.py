"""Capability allowlist applied immediately before send_document.

A schema-valid address is not enough. An untrusted recipient is allowed when
that address is a reader of the document and a reader on the value's
capability tag. A literal the user wrote in the request is a separate allow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Bound:
    value: Any
    trusted: bool
    sources: tuple[str, ...]
    readers: tuple[str, ...]


def allow_send(
    document_id: str,
    recipient: str,
    recipient_bound: Bound,
    documents: dict[str, dict[str, Any]],
) -> tuple[bool, str]:
    if not isinstance(recipient, str) or recipient == "":
        return False, "recipient_missing"
    document = documents.get(document_id)
    if document is None:
        return False, "unknown_document"
    if recipient_bound.trusted and "user" in recipient_bound.sources:
        return True, "user_literal"
    document_readers = tuple(document["readers"])
    if recipient in document_readers and recipient in recipient_bound.readers:
        return True, "reader"
    return False, "recipient_not_allowed"
