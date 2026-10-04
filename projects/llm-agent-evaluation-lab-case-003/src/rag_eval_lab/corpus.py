"""Load the synthetic corpus and enforce construction quotas."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .errors import LabInputError
from .models import LABELS, BlockedSpan, Chunk, Claim, Corpus, Evidence, Item
from .textutil import find_span


def _as_tuple(values: Any, label: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise LabInputError(f"{label} must be a list of strings")
    return tuple(values)


def _span(text: str, substring: str, label: str) -> tuple[int, int]:
    try:
        return find_span(text, substring)
    except ValueError as exc:
        raise LabInputError(f"{label}: {exc}") from exc


def _load_claims(raw: Any) -> dict[str, Claim]:
    if not isinstance(raw, list):
        raise LabInputError("claims must be a list")
    claims: dict[str, Claim] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise LabInputError("claim entries must be objects")
        claim_id = entry.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            raise LabInputError("claim_id is required")
        polarity = entry.get("polarity")
        if polarity not in {"true", "false"}:
            raise LabInputError(f"{claim_id}: polarity must be true or false")
        premises = _as_tuple(entry.get("premises") or [], f"{claim_id} premises")
        contradicts = _as_tuple(entry.get("contradicts") or [], f"{claim_id} contradicts")
        text = entry.get("text")
        if not isinstance(text, str) or not text:
            raise LabInputError(f"{claim_id}: text is required")
        claims[claim_id] = Claim(
            claim_id=claim_id,
            text=text,
            polarity=polarity,
            contradicts=frozenset(contradicts),
            premises=premises,
        )
    for claim in claims.values():
        for other in claim.contradicts:
            if other not in claims:
                raise LabInputError(f"{claim.claim_id} contradicts unknown {other}")
        for premise in claim.premises:
            if premise not in claims:
                raise LabInputError(f"{claim.claim_id} premise unknown {premise}")
    symmetrized: dict[str, Claim] = {}
    for claim in claims.values():
        extra = set(claim.contradicts)
        for other in claims.values():
            if claim.claim_id in other.contradicts:
                extra.add(other.claim_id)
        extra.discard(claim.claim_id)
        symmetrized[claim.claim_id] = Claim(
            claim_id=claim.claim_id,
            text=claim.text,
            polarity=claim.polarity,
            contradicts=frozenset(extra),
            premises=claim.premises,
        )
    return symmetrized


def _load_chunks(raw: Any) -> dict[str, Chunk]:
    if not isinstance(raw, list):
        raise LabInputError("chunks must be a list")
    chunks: dict[str, Chunk] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise LabInputError("chunk entries must be objects")
        chunk_id = entry.get("chunk_id")
        text = entry.get("text")
        if not isinstance(chunk_id, str) or not isinstance(text, str) or not text:
            raise LabInputError("chunk_id and text are required")
        evidences: list[Evidence] = []
        for spec in entry.get("evidences") or []:
            if not isinstance(spec, dict):
                raise LabInputError(f"{chunk_id}: evidence must be an object")
            claim_id = spec.get("claim_id")
            substring = spec.get("substring")
            if not isinstance(claim_id, str) or not isinstance(substring, str):
                raise LabInputError(f"{chunk_id}: evidence claim_id and substring")
            start, end = _span(text, substring, chunk_id)
            evidences.append(Evidence(claim_id, chunk_id, start, end))
        blocked: list[BlockedSpan] = []
        for spec in entry.get("blocked") or []:
            if not isinstance(spec, dict):
                raise LabInputError(f"{chunk_id}: blocked must be an object")
            tag = spec.get("tag")
            substring = spec.get("substring")
            if tag not in {"poison", "counterfactual"} or not isinstance(substring, str):
                raise LabInputError(f"{chunk_id}: blocked tag or substring")
            start, end = _span(text, substring, chunk_id)
            blocked.append(BlockedSpan(tag, start, end))
        tags = _as_tuple(entry.get("tags") or [], f"{chunk_id} tags")
        chunks[chunk_id] = Chunk(
            chunk_id=chunk_id,
            doc_id=str(entry.get("doc_id") or chunk_id),
            text=text,
            tags=tags,
            blocked=tuple(blocked),
            evidences=tuple(evidences),
            indexed=bool(entry.get("indexed", True)),
        )
    return chunks


def _load_items(raw: Any, claims: dict[str, Claim], chunks: dict[str, Chunk]) -> list[Item]:
    if not isinstance(raw, list):
        raise LabInputError("items must be a list")
    items: list[Item] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise LabInputError("item entries must be objects")
        item_id = entry.get("item_id")
        question = entry.get("question")
        label = entry.get("construction_label")
        batch = entry.get("batch")
        if not all(isinstance(value, str) and value for value in (item_id, question, label, batch)):
            raise LabInputError("item_id, question, construction_label, and batch are required")
        if label not in LABELS:
            raise LabInputError(f"{item_id}: unknown construction_label")
        gold = _as_tuple(entry.get("gold_claim_ids") or [], f"{item_id} gold")
        for claim_id in gold:
            if claim_id not in claims:
                raise LabInputError(f"{item_id}: unknown gold claim {claim_id}")
        required = _as_tuple(entry.get("required_chunk_ids") or [], f"{item_id} required")
        for chunk_id in required:
            if chunk_id not in chunks:
                raise LabInputError(f"{item_id}: unknown chunk {chunk_id}")
        statement_first = entry.get("statement_first")
        if statement_first is not None and not isinstance(statement_first, dict):
            raise LabInputError(f"{item_id}: statement_first must be an object or null")
        model_label = entry.get("model_label")
        if model_label is not None and model_label not in LABELS:
            raise LabInputError(f"{item_id}: model_label")
        items.append(
            Item(
                item_id=item_id,
                question=question,
                construction_label=label,
                batch=batch,
                gold_claim_ids=gold,
                required_chunk_ids=required,
                outside_memory=bool(entry.get("outside_memory", True)),
                slice_name=str(entry.get("slice") or "clean"),
                poison_target=bool(entry.get("poison_target", False)),
                model_label=model_label,
                statement_first=statement_first,
                positive_chunk_ids=_as_tuple(entry.get("positive_chunk_ids") or [], "positives"),
                negative_chunk_ids=_as_tuple(entry.get("negative_chunk_ids") or [], "negatives"),
                false_claim_ids=_as_tuple(entry.get("false_claim_ids") or [], "false claims"),
                forced_chunk_ids=_as_tuple(entry.get("forced_chunk_ids") or [], "forced"),
            )
        )
    return items


def validate_statement_first(item: Item, chunks: dict[str, Chunk]) -> None:
    """Statement-first traces are required on scored non-adversarial items."""
    blob = item.statement_first
    if item.batch == "one_shot":
        if blob is not None:
            raise LabInputError(f"{item.item_id}: one-shot control must not use statement-first")
        return
    if item.batch != "scored":
        return
    if item.construction_label == "unanswerable":
        if blob is not None:
            raise LabInputError(f"{item.item_id}: unanswerable items are built as topical negatives")
        return
    if not isinstance(blob, dict):
        raise LabInputError(f"{item.item_id}: statement_first is required")
    for key, count in (
        ("factual_statements", 1),
        ("summary_statements", 3),
        ("conclusion_statements", 3),
        ("sampled_statements", 1),
    ):
        values = blob.get(key)
        if not isinstance(values, list) or len(values) < count:
            raise LabInputError(f"{item.item_id}: {key} is short")
        if not all(isinstance(value, str) and value for value in values):
            raise LabInputError(f"{item.item_id}: {key} must be strings")
    if not isinstance(blob.get("theme"), str) or not blob["theme"]:
        raise LabInputError(f"{item.item_id}: theme is required")
    sampled = blob["sampled_statements"]
    texts = [chunk.text for chunk in chunks.values()]

    def in_corpus(statement: str) -> bool:
        return any(statement in text for text in texts)

    if item.construction_label == "fact_single":
        if len(sampled) != 1 or not in_corpus(sampled[0]):
            raise LabInputError(f"{item.item_id}: fact_single samples one corpus span")
    elif item.construction_label == "summary":
        if len(sampled) < 2 or not all(in_corpus(statement) for statement in sampled):
            raise LabInputError(f"{item.item_id}: summary samples two or more corpus spans")
    elif item.construction_label == "reasoning":
        conclusions = set(blob["conclusion_statements"])
        if any(statement not in conclusions for statement in sampled):
            raise LabInputError(f"{item.item_id}: reasoning samples conclusion statements")
        if any(in_corpus(statement) for statement in sampled):
            raise LabInputError(f"{item.item_id}: reasoning answer must not be a stored span")


def quota_counts(items: list[Item]) -> dict[str, int]:
    counts = Counter(item.construction_label for item in items if item.batch == "scored")
    return {label: int(counts.get(label, 0)) for label in LABELS}


def assert_quotas(items: list[Item], quotas: dict[str, int]) -> None:
    actual = quota_counts(items)
    if actual != quotas:
        raise LabInputError(f"quota mismatch: built {actual} manifest {quotas}")


def is_fact_single_heavy(items: list[Item], threshold: float = 0.8) -> bool:
    if not items:
        return False
    facts = sum(1 for item in items if item.construction_label == "fact_single")
    return facts / len(items) >= threshold


def reject_if_collapsed(items: list[Item], quotas: dict[str, int]) -> None:
    """A one-shot batch that collapses to fact_single is not a scored set."""
    target = quotas.get("fact_single", 0) / max(1, sum(quotas.values()))
    if is_fact_single_heavy(items) and target < 0.8:
        raise LabInputError("generation batch collapsed to fact_single")


def select_scored(items: list[Item]) -> list[Item]:
    return [item for item in items if item.batch == "scored"]


def load_corpus_dict(payload: dict[str, Any]) -> Corpus:
    if not isinstance(payload, dict):
        raise LabInputError("corpus must be an object")
    claims = _load_claims(payload.get("claims"))
    chunks = _load_chunks(payload.get("chunks"))
    for chunk in chunks.values():
        for evidence in chunk.evidences:
            if evidence.claim_id not in claims:
                raise LabInputError(f"{chunk.chunk_id}: unknown evidence {evidence.claim_id}")
    items = _load_items(payload.get("items"), claims, chunks)
    for item in items:
        validate_statement_first(item, chunks)
    quotas = payload.get("quotas")
    if not isinstance(quotas, dict):
        raise LabInputError("quotas object is required")
    quota_map = {label: int(quotas[label]) for label in LABELS}
    assert_quotas(items, quota_map)
    regression = _as_tuple(payload.get("regression_item_ids") or [], "regression_item_ids")
    known = {item.item_id for item in items}
    for item_id in regression:
        if item_id not in known:
            raise LabInputError(f"unknown regression item {item_id}")
    memory = frozenset(_as_tuple(payload.get("provider_memory_claim_ids") or [], "memory"))
    for claim_id in memory:
        if claim_id not in claims:
            raise LabInputError(f"unknown memory claim {claim_id}")
    order = tuple(item.item_id for item in items)
    return Corpus(
        claims=claims,
        chunks=chunks,
        items={item.item_id: item for item in items},
        quotas=quota_map,
        regression_item_ids=regression,
        provider_memory_claim_ids=memory,
        item_order=order,
    )


def load_corpus(path: Path) -> Corpus:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LabInputError(f"missing corpus file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LabInputError(f"malformed corpus file: {path}") from exc
    return load_corpus_dict(payload)
