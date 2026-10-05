"""Tie finish claims to named tool-result fields."""

from __future__ import annotations

from typing import Any


def ground_claims(claims: list[dict[str, Any]], tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grounded = []
    for claim in claims:
        field = claim.get("evidence_field")
        value = claim.get("value")
        entailed = False
        if isinstance(field, str):
            for result in tool_results:
                if result["fields"].get(field) == value:
                    entailed = True
                    break
        support = "entailed" if entailed else "unverified"
        grounded.append(
            {
                "claim_id": claim["claim_id"],
                "value": value,
                "evidence_field": field,
                "support": support,
            }
        )
    return grounded
