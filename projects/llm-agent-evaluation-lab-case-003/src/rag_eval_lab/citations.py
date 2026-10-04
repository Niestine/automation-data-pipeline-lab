"""ALCE citation recall and precision on atomic claims, plus confabulation."""

from __future__ import annotations

from typing import Any, Callable

from .textutil import near_copy

MAX_CITATIONS = 3


def score_citations(
    spans: list[Any],
    entailed_by: Callable[[list[Any]], bool],
) -> dict[str, Any]:
    """Citation recall is 1 only when the cited spans together entail the claim.

    A citation is irrelevant when it does not entail the claim alone and the
    other citations still do. Recall 0 forces every citation precision to 0.
    A fourth citation is a precision defect. Redundant full supports stay precise.
    """
    if not spans:
        return {
            "recall": 0,
            "precision": 0.0,
            "citations": [],
            "extra_defects": 0,
        }
    recall = 1 if entailed_by(spans) else 0
    rows: list[dict[str, Any]] = []
    extra = 0
    for index, span in enumerate(spans):
        alone = entailed_by([span])
        others = [other for other_index, other in enumerate(spans) if other_index != index]
        others_entail = entailed_by(others) if others else False
        irrelevant = (not alone) and others_entail
        defect = index >= MAX_CITATIONS
        if defect:
            extra += 1
        if recall == 0 or irrelevant or defect:
            precision = 0
        else:
            precision = 1
        rows.append(
            {
                "index": index,
                "precision": precision,
                "irrelevant": irrelevant,
                "extra_defect": defect,
                "alone": alone,
            }
        )
    if recall == 0:
        claim_precision = 0.0
    else:
        claim_precision = sum(row["precision"] for row in rows) / len(rows)
    return {
        "recall": recall,
        "precision": claim_precision,
        "citations": rows,
        "extra_defects": extra,
    }


def anti_copy(surface: str, retrieved_texts: list[str]) -> bool:
    return any(near_copy(surface, text) for text in retrieved_texts)


def confabulation_parts(claims: list[dict[str, Any]]) -> dict[str, int]:
    """Numerator is the sum of three buckets, so one claim can count twice.

    Buckets: unsupported label, contradicted label, and participation in an
    in-answer contradiction. Abstain claims sit in the denominator only.
    """
    present = {claim["claim_id"] for claim in claims if claim.get("claim_id")}
    unsupported = 0
    contradicted = 0
    internal = 0
    for claim in claims:
        support = claim.get("resolved_support")
        if support == "unsupported":
            unsupported += 1
        if support == "contradicted":
            contradicted += 1
        claim_id = claim.get("claim_id")
        conflicts = set(claim.get("contradicts") or ())
        if claim_id and conflicts & present:
            internal += 1
    return {
        "unsupported": unsupported,
        "contradicted": contradicted,
        "internal": internal,
        "numerator": unsupported + contradicted + internal,
        "denominator": len(claims),
    }


def citation_defect(kind: str, citation_recall: int, resolved_support: str) -> bool:
    """Missing or non-entailing citation on a fact claim. Abstain is excluded."""
    if kind != "fact" or resolved_support == "abstain":
        return False
    return citation_recall == 0
