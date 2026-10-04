"""Datatypes for the synthetic handbook corpus and scored items."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


LABELS = ("fact_single", "summary", "reasoning", "unanswerable")
CANONICAL_REFUSAL = (
    "The handbook does not contain enough information to answer this question."
)
# Papers showed these do not separate question-similar poison from ordinary prose.
DEFENSES_NOT_ON_ACCEPTANCE_PATH = (
    "perplexity_threshold",
    "query_paraphrase",
    "duplicate_text_filter",
)
# The checks `resolve_claims` and `decide_acceptance` actually apply, in order.
ACCEPTANCE_CHECKS = (
    "schema",
    "citation_bounds",
    "blocked_span",
    "gold_contradiction",
    "internal_contradiction",
    "citation_entailment",
)


@dataclass
class Evidence:
    claim_id: str
    chunk_id: str
    start: int
    end: int


@dataclass
class BlockedSpan:
    tag: str
    start: int
    end: int


@dataclass
class Claim:
    claim_id: str
    text: str
    polarity: str
    contradicts: frozenset[str]
    premises: tuple[str, ...]


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    tags: tuple[str, ...]
    blocked: tuple[BlockedSpan, ...]
    evidences: tuple[Evidence, ...]
    indexed: bool = True


@dataclass
class Item:
    item_id: str
    question: str
    construction_label: str
    batch: str
    gold_claim_ids: tuple[str, ...]
    required_chunk_ids: tuple[str, ...]
    outside_memory: bool
    slice_name: str
    poison_target: bool
    model_label: str | None
    statement_first: dict[str, Any] | None
    positive_chunk_ids: tuple[str, ...] = ()
    negative_chunk_ids: tuple[str, ...] = ()
    false_claim_ids: tuple[str, ...] = ()
    forced_chunk_ids: tuple[str, ...] = ()


@dataclass
class Corpus:
    claims: dict[str, Claim]
    chunks: dict[str, Chunk]
    items: dict[str, Item]
    quotas: dict[str, int]
    regression_item_ids: tuple[str, ...]
    provider_memory_claim_ids: frozenset[str]
    item_order: tuple[str, ...] = field(default_factory=tuple)

    def claim(self, claim_id: str) -> Claim:
        return self.claims[claim_id]

    def chunk(self, chunk_id: str) -> Chunk:
        return self.chunks[chunk_id]

    def ordered_items(self) -> list[Item]:
        return [self.items[item_id] for item_id in self.item_order]

    def chunk_entails(self, chunk_id: str, claim_id: str) -> bool:
        chunk = self.chunks.get(chunk_id)
        if chunk is None:
            return False
        return any(evidence.claim_id == claim_id for evidence in chunk.evidences)

    def gold_claim_retrieved(self, claim_id: str, retrieved_ids: list[str]) -> bool:
        claim = self.claims[claim_id]
        if claim.premises:
            return all(
                any(self.chunk_entails(chunk_id, premise) for chunk_id in retrieved_ids)
                for premise in claim.premises
            )
        return any(self.chunk_entails(chunk_id, claim_id) for chunk_id in retrieved_ids)

    def chunk_is_relevant(self, chunk_id: str, gold_ids: tuple[str, ...] | list[str]) -> bool:
        for gold_id in gold_ids:
            claim = self.claims[gold_id]
            if claim.premises:
                if any(self.chunk_entails(chunk_id, premise) for premise in claim.premises):
                    return True
            elif self.chunk_entails(chunk_id, gold_id):
                return True
        return False

    def evidences_for(self, claim_id: str) -> list[Evidence]:
        found: list[Evidence] = []
        for chunk in self.chunks.values():
            for evidence in chunk.evidences:
                if evidence.claim_id == claim_id:
                    found.append(evidence)
        return found
