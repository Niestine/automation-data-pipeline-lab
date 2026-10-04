"""Resolve citations, support labels, and per-item metrics."""

from __future__ import annotations

import copy
from typing import Any

from .citations import anti_copy, citation_defect, confabulation_parts, score_citations
from .diagnostics import answer_relevance, context_relevance, diagnose, split_sentences
from .errors import LabInputError
from .models import CANONICAL_REFUSAL, ACCEPTANCE_CHECKS, Corpus, Evidence, Item
from .retrieve import Hit
from .schema_gate import citation_bound_errors, validate_response


def materialize_payload(payload: dict[str, Any], corpus: Corpus) -> tuple[dict[str, Any], list[str]]:
    """Replace fixture substrings with integer offsets before schema validation."""
    data = copy.deepcopy(payload)
    errors: list[str] = []
    claims = data.get("claims")
    if not isinstance(claims, list):
        return data, errors
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue
        citations = claim.get("citations")
        if not isinstance(citations, list):
            continue
        resolved: list[Any] = []
        for cite in citations:
            if not isinstance(cite, dict) or "substring" not in cite:
                resolved.append(cite)
                continue
            chunk_id = cite.get("chunk_id")
            substring = cite.get("substring")
            chunk = corpus.chunks.get(chunk_id) if isinstance(chunk_id, str) else None
            if chunk is None or not isinstance(substring, str):
                errors.append(f"$.claims[{index}]: substring citation is unresolved")
                resolved.append({"chunk_id": chunk_id, "start": 0, "end": 0})
                continue
            start = chunk.text.find(substring)
            if start < 0:
                errors.append(f"$.claims[{index}]: substring not in chunk")
                resolved.append({"chunk_id": chunk_id, "start": 0, "end": 0})
                continue
            resolved.append(
                {"chunk_id": chunk_id, "start": start, "end": start + len(substring)}
            )
        claim["citations"] = resolved
    return data, errors


def contract_errors(payload: Any, corpus: Corpus, item_id: str) -> list[str]:
    if not isinstance(payload, dict):
        return ["$: payload must be an object"]
    errors = validate_response(payload)
    if errors:
        return errors
    if payload.get("item_id") != item_id:
        errors.append("$.item_id: mismatch")
    lengths = {chunk_id: len(chunk.text) for chunk_id, chunk in corpus.chunks.items()}
    errors.extend(citation_bound_errors(payload, lengths))
    return errors


def _covers(span: dict[str, Any], evidence: Evidence) -> bool:
    return (
        span.get("chunk_id") == evidence.chunk_id
        and isinstance(span.get("start"), int)
        and isinstance(span.get("end"), int)
        and span["start"] <= evidence.start
        and span["end"] >= evidence.end
    )


def _fact_entailed(spans: list[dict[str, Any]], evidences: list[Evidence]) -> bool:
    if not evidences:
        return False
    return any(_covers(span, evidence) for span in spans for evidence in evidences)


def _inference_entailed(spans: list[dict[str, Any]], corpus: Corpus, premises: tuple[str, ...]) -> bool:
    for premise in premises:
        if not _fact_entailed(spans, corpus.evidences_for(premise)):
            return False
    return True


def _citations_entail(spans: list[dict[str, Any]], corpus: Corpus, claim_id: str | None) -> bool:
    if not claim_id or claim_id not in corpus.claims:
        return False
    claim = corpus.claims[claim_id]
    if claim.premises:
        return _inference_entailed(spans, corpus, claim.premises)
    return _fact_entailed(spans, corpus.evidences_for(claim_id))


def _blocked_hit(corpus: Corpus, span: dict[str, Any]) -> str | None:
    chunk = corpus.chunks.get(span.get("chunk_id"))
    if chunk is None:
        return None
    start = span.get("start")
    end = span.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    for region in chunk.blocked:
        if start < region.end and region.start < end:
            return region.tag
    return None


def _fallback_units(
    claim: dict[str, Any],
    retrieved: set[str],
) -> list[dict[str, Any]]:
    fallback = claim.get("fallback") or {}
    statements = fallback.get("statements") if isinstance(fallback, dict) else None
    if not isinstance(statements, list) or not statements:
        return [
            {
                "claim_id": None,
                "entailed_by": set(),
                "support_hint": "unsupported",
            }
        ]
    units = []
    for statement in statements:
        verdict = statement.get("verdict")
        chunk_id = statement.get("chunk_id")
        entailed: set[str] = set()
        if verdict == "yes" and isinstance(chunk_id, str) and chunk_id in retrieved:
            entailed.add(chunk_id)
        units.append(
            {
                "claim_id": None,
                "entailed_by": entailed,
                "support_hint": "supported" if entailed else "unsupported",
            }
        )
    return units


def resolve_claims(
    corpus: Corpus,
    item: Item,
    payload: dict[str, Any],
    hits: list[Hit],
) -> dict[str, Any]:
    retrieved_ids = [hit.chunk_id for hit in hits]
    retrieved_set = set(retrieved_ids)
    relevant = {
        chunk_id
        for chunk_id in retrieved_ids
        if corpus.chunk_is_relevant(chunk_id, item.gold_claim_ids)
    }
    gold_retrieved = {
        claim_id
        for claim_id in item.gold_claim_ids
        if corpus.gold_claim_retrieved(claim_id, retrieved_ids)
    }
    resolved_rows: list[dict[str, Any]] = []
    response_ids: list[str | None] = []
    entailed_rows: list[set[str]] = []
    fail_closed = False
    fail_reason: str | None = None
    claim_schema_defects = 0

    for claim in payload["claims"]:
        citations = list(claim.get("citations") or [])
        claim_id = claim.get("claim_id")
        kind = claim.get("kind")
        model_support = claim.get("support")
        blocked_tag = None
        for span in citations:
            blocked_tag = _blocked_hit(corpus, span)
            if blocked_tag:
                break
        contradicts_gold = False
        contradicts: frozenset[str] = frozenset()
        units: list[dict[str, Any]] = []
        if isinstance(claim_id, str) and claim_id in corpus.claims:
            contradicts = corpus.claims[claim_id].contradicts
            contradicts_gold = bool(contradicts & set(item.gold_claim_ids))

        if blocked_tag:
            support = "contradicted"
            fail_closed = True
            fail_reason = fail_reason or blocked_tag
        elif contradicts_gold:
            support = "contradicted"
            fail_closed = True
            fail_reason = fail_reason or "gold_contradiction"
        elif kind == "insufficient_evidence" or model_support == "abstain":
            if citations:
                support = "unsupported"
            else:
                support = "abstain"
        elif model_support in {"supported", "partial"} and not citations:
            support = "abstain"
            claim_schema_defects += 1
        elif claim_id is None:
            units = _fallback_units(claim, retrieved_set)
            support = (
                "supported"
                if units and all(unit["support_hint"] == "supported" for unit in units)
                else "unsupported"
            )
        else:
            entailed = _citations_entail(citations, corpus, claim_id)
            if entailed and corpus.claims[claim_id].premises:
                support = "partial"
            elif entailed:
                support = "supported"
            else:
                support = "unsupported"

        citation_score = score_citations(
            citations,
            lambda spans, cid=claim_id: _citations_entail(list(spans), corpus, cid),
        )
        # Abstain is a disposition, not a generated claim that can be faithful or hallucinated.
        if support != "abstain":
            if claim_id is None:
                units = units or _fallback_units(claim, retrieved_set)
                for unit in units:
                    response_ids.append(None)
                    entailed_rows.append(set(unit["entailed_by"]))
            else:
                context_chunks = _context_chunks(corpus, claim_id, retrieved_ids)
                response_ids.append(claim_id)
                entailed_rows.append(context_chunks)

        resolved_rows.append(
            {
                "claim_id": claim_id,
                "text": claim.get("text"),
                "kind": kind,
                "model_support": model_support,
                "resolved_support": support,
                "contradicts": contradicts,
                "citation_recall": citation_score["recall"],
                "citation_precision": citation_score["precision"],
                "citation_rows": citation_score["citations"],
                "extra_defects": citation_score["extra_defects"],
                "adopted_false": model_support == "supported" and (
                    contradicts_gold or blocked_tag is not None
                ),
            }
        )

    present_ids = {row["claim_id"] for row in resolved_rows if row["claim_id"]}
    internal = False
    for row in resolved_rows:
        if row["claim_id"] and set(row["contradicts"]) & present_ids:
            internal = True
            break
    if internal:
        fail_closed = True
        fail_reason = fail_reason or "internal_contradiction"

    metrics = diagnose(
        list(item.gold_claim_ids),
        response_ids,
        entailed_rows,
        retrieved_ids,
        relevant,
        gold_retrieved,
    )
    parts = confabulation_parts(resolved_rows)
    fact_claims = 0
    defects = 0
    for row in resolved_rows:
        if citation_defect(row["kind"], row["citation_recall"], row["resolved_support"]):
            defects += 1
        if row["kind"] == "fact" and row["resolved_support"] != "abstain":
            fact_claims += 1
    structured = _structured_abstain(resolved_rows)
    copied = anti_copy(payload.get("surface_text") or "", [hit.text for hit in hits])
    relevance = answer_relevance(item.question, list(payload.get("reverse_questions") or []))
    sentences: list[str] = []
    useful: list[bool] = []
    for hit in hits:
        for sentence in split_sentences(hit.text):
            sentences.append(sentence)
            useful.append(_sentence_useful(corpus, hit.chunk_id, sentence, item.gold_claim_ids))
    insufficient = structured and not gold_retrieved
    ctx_rel = context_relevance(sentences, useful, insufficient)
    accurate = _accurate(item, resolved_rows, structured, fail_closed)
    acceptance = decide_acceptance(
        scored=True,
        fail_closed=fail_closed,
        structured_abstain=structured,
        accurate=accurate,
        rows=resolved_rows,
        copied=copied,
        metrics=metrics,
    )
    return {
        "scored": True,
        "metrics": metrics,
        "rows": resolved_rows,
        "fail_closed": fail_closed,
        "fail_reason": fail_reason,
        "confab": parts,
        "citation_defects": defects,
        "fact_claims": fact_claims,
        "structured_abstain": structured,
        "surface_refusal_match": (payload.get("surface_text") or "").strip() == CANONICAL_REFUSAL,
        "anti_copy": copied,
        "answer_relevance": relevance,
        "context_relevance": ctx_rel,
        "accurate": accurate,
        "acceptance": acceptance,
        "claim_schema_defects": claim_schema_defects,
        "gold_retrieved": sorted(gold_retrieved),
        "retrieved": retrieved_ids,
        "poison_in_context": any("poison" in hit.tags for hit in hits),
        "checks": ACCEPTANCE_CHECKS,
    }


def _context_chunks(corpus: Corpus, claim_id: str, retrieved_ids: list[str]) -> set[str]:
    claim = corpus.claims.get(claim_id)
    if claim is None:
        return set()
    if claim.premises:
        if not corpus.gold_claim_retrieved(claim_id, retrieved_ids):
            return set()
        holders = set()
        for chunk_id in retrieved_ids:
            if any(corpus.chunk_entails(chunk_id, premise) for premise in claim.premises):
                holders.add(chunk_id)
        return holders
    return {chunk_id for chunk_id in retrieved_ids if corpus.chunk_entails(chunk_id, claim_id)}


def _sentence_useful(corpus: Corpus, chunk_id: str, sentence: str, gold_ids: tuple[str, ...]) -> bool:
    chunk = corpus.chunks[chunk_id]
    start = chunk.text.find(sentence)
    if start < 0:
        return False
    end = start + len(sentence)
    for gold_id in gold_ids:
        claim = corpus.claims[gold_id]
        targets = claim.premises or (gold_id,)
        for target in targets:
            for evidence in chunk.evidences:
                if evidence.claim_id != target:
                    continue
                if start <= evidence.start and end >= evidence.end:
                    return True
    return False


def _structured_abstain(rows: list[dict[str, Any]]) -> bool:
    if len(rows) != 1:
        return False
    row = rows[0]
    return (
        row["kind"] == "insufficient_evidence"
        and row["resolved_support"] == "abstain"
        and row["citation_recall"] == 0
        and not row["citation_rows"]
    )


def _accurate(item: Item, rows: list[dict[str, Any]], structured: bool, fail_closed: bool) -> bool:
    if fail_closed:
        return False
    if not item.gold_claim_ids:
        return structured
    gold = set(item.gold_claim_ids)
    good = {
        row["claim_id"]
        for row in rows
        if row["claim_id"] in gold and row["resolved_support"] in {"supported", "partial"}
    }
    bad = [
        row
        for row in rows
        if row["claim_id"] not in gold or row["resolved_support"] in {"unsupported", "contradicted", "abstain"}
    ]
    return good == gold and not bad


def decide_acceptance(
    *,
    scored: bool,
    fail_closed: bool,
    structured_abstain: bool,
    accurate: bool,
    rows: list[dict[str, Any]],
    copied: bool,
    metrics: dict[str, Any] | None,
) -> str:
    """auto_accept, require_approval, or reject. Model text cannot loosen a reject."""
    if not scored or fail_closed:
        return "reject"
    if any(row["resolved_support"] in {"unsupported", "contradicted"} for row in rows):
        return "reject"
    if structured_abstain and accurate:
        return "auto_accept"
    needs_review = copied
    if metrics is not None:
        if (metrics.get("self_knowledge") or 0) > 0:
            needs_review = True
        if (metrics.get("relevant_noise") or 0) > 0 or (metrics.get("irrelevant_noise") or 0) > 0:
            needs_review = True
        recall = metrics.get("recall")
        if recall is not None and recall < 1:
            needs_review = True
    for row in rows:
        if row["resolved_support"] == "partial":
            needs_review = True
        if row["citation_recall"] == 1 and row["citation_precision"] < 1:
            needs_review = True
        if row["extra_defects"]:
            needs_review = True
        if row["kind"] == "fact" and row["resolved_support"] == "supported" and row["citation_recall"] == 0:
            needs_review = True
    if accurate and not needs_review:
        return "auto_accept"
    return "require_approval"


def error_detection(
    rows: list[dict[str, Any]],
    false_ids: set[str],
    true_ids: set[str],
) -> dict[str, bool]:
    """Model-side detection is separate from the guardrail that blocks the span.

    Detection: the model does not mark the false claim supported, and it abstains
    or labels the document contradicted. Correction also states the planted truth.
    """
    adopted = any(
        row["claim_id"] in false_ids and row["model_support"] == "supported" for row in rows
    )
    marked = any(row["model_support"] in {"abstain", "contradicted"} for row in rows) or (
        len(rows) == 1 and rows[0]["kind"] == "insufficient_evidence"
    )
    detection = (not adopted) and marked
    stated = any(row["claim_id"] in true_ids for row in rows)
    return {
        "detection": detection,
        "correction": detection and stated,
        "adopted_false": adopted,
    }


def unscored_abstain(item_id: str, attempts: int) -> dict[str, Any]:
    return {
        "scored": False,
        "metrics": None,
        "rows": [],
        "fail_closed": True,
        "fail_reason": "schema_failure",
        "confab": {"numerator": 0, "denominator": 0, "unsupported": 0, "contradicted": 0, "internal": 0},
        "citation_defects": 0,
        "fact_claims": 0,
        "structured_abstain": True,
        "surface_refusal_match": False,
        "anti_copy": False,
        "answer_relevance": None,
        "context_relevance": None,
        "accurate": False,
        "acceptance": "reject",
        "claim_schema_defects": 1,
        "gold_retrieved": [],
        "retrieved": [],
        "poison_in_context": False,
        "abstain_reason": "schema_failure",
        "item_id": item_id,
        "attempts": attempts,
        "checks": ACCEPTANCE_CHECKS,
    }


def require_stratified(report: dict[str, Any]) -> None:
    by_label = report.get("by_label")
    if not isinstance(by_label, dict) or not by_label:
        raise LabInputError("report must include per-label metrics")
    for label in ("fact_single", "summary", "reasoning", "unanswerable"):
        if label not in by_label:
            raise LabInputError(f"report is missing label {label}")
