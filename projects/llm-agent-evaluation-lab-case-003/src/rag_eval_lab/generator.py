"""Scripted and context-trusting generators. No hosted model is called."""

from __future__ import annotations

import copy
from typing import Any

from .retrieve import Hit


class FakeGenerator:
    """Returns the next scripted payload for an item. Missing steps are invalid."""

    def __init__(self, script: dict[str, list[dict[str, Any]]], provider_id: str = "fake-script-v1") -> None:
        self.script = script
        self.provider_id = provider_id
        self.calls = 0

    def complete(self, item_id: str, attempt: int, hits: list[Hit], prompt: str) -> dict[str, Any]:
        del hits, prompt
        self.calls += 1
        steps = self.script.get(item_id) or []
        if attempt < len(steps):
            return copy.deepcopy(steps[attempt])
        return {"schema_version": "cited_answer_v1", "unexpected": True}


class SequenceGenerator:
    """Test double that replays one payload list for every item."""

    def __init__(self, payloads: list[dict[str, Any]], provider_id: str = "sequence") -> None:
        self.payloads = payloads
        self.provider_id = provider_id
        self.calls = 0

    def complete(self, item_id: str, attempt: int, hits: list[Hit], prompt: str) -> dict[str, Any]:
        del item_id, hits, prompt
        self.calls += 1
        if attempt < len(self.payloads):
            return copy.deepcopy(self.payloads[attempt])
        return {"schema_version": "cited_answer_v1", "unexpected": True}


class TrustingGenerator:
    """Frozen generator: copy every planted claim in the retrieved chunks, plus one hallucination.

    The same policy is used for every k and chunk size. Metric movement comes from
    which claims the retriever placed in context.
    """

    provider_id = "trusting-v1"

    def __init__(self, evidences: dict[str, list[tuple[str, int, int]]]) -> None:
        self.evidences = evidences
        self.calls = 0

    def complete(self, item_id: str, attempt: int, hits: list[Hit], prompt: str) -> dict[str, Any]:
        del attempt, prompt
        self.calls += 1
        claims: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hit in hits:
            for claim_id, start, end in self.evidences.get(hit.chunk_id, []):
                if claim_id in seen:
                    continue
                seen.add(claim_id)
                claims.append(
                    {
                        "claim_id": claim_id,
                        "text": claim_id,
                        "kind": "fact",
                        "support": "supported",
                        "citations": [{"chunk_id": hit.chunk_id, "start": start, "end": end}],
                        "fallback": None,
                    }
                )
        claims.append(
            {
                "claim_id": None,
                "text": "The depot was founded in 1888.",
                "kind": "fact",
                "support": "unsupported",
                "citations": [],
                "fallback": {
                    "statements": [
                        {
                            "text": "The depot was founded in 1888.",
                            "explanation": "No retrieved sentence states a founding year.",
                            "verdict": "no",
                            "chunk_id": None,
                        }
                    ]
                },
            }
        )
        return {
            "schema_version": "cited_answer_v1",
            "item_id": item_id,
            "surface_text": " ".join(claim["text"] for claim in claims),
            "reverse_questions": ["beacon status"],
            "claims": claims,
        }


class TerseGenerator:
    """Frozen generator: cite the first planted claim of the top-ranked chunk and stop.

    Paired with `TrustingGenerator` on the same retrieval so the comparison board
    shows a precision/recall trade-off rather than a winner.
    """

    provider_id = "terse-v1"

    def __init__(self, evidences: dict[str, list[tuple[str, int, int]]]) -> None:
        self.evidences = evidences
        self.calls = 0

    def complete(self, item_id: str, attempt: int, hits: list[Hit], prompt: str) -> dict[str, Any]:
        del attempt, prompt
        self.calls += 1
        top = self.evidences.get(hits[0].chunk_id, []) if hits else []
        if top:
            claim_id, start, end = top[0]
            claim = {
                "claim_id": claim_id,
                "text": claim_id,
                "kind": "fact",
                "support": "supported",
                "citations": [{"chunk_id": hits[0].chunk_id, "start": start, "end": end}],
                "fallback": None,
            }
        else:
            claim = {
                "claim_id": None,
                "text": "The retrieved chunks do not contain enough information.",
                "kind": "insufficient_evidence",
                "support": "abstain",
                "citations": [],
                "fallback": None,
            }
        return {
            "schema_version": "cited_answer_v1",
            "item_id": item_id,
            "surface_text": claim["text"],
            "reverse_questions": ["beacon status"],
            "claims": [claim],
        }
