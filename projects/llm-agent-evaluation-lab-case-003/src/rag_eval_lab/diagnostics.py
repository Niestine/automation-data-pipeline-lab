"""Planted-claim diagnostic ratios.

Entailment is membership of a planted claim in a retrieved chunk, passed in
by the caller. Unplanted statements never increase gold recall.
"""

from __future__ import annotations

from typing import Any

from .textutil import harmonic, mean_cosine


def diagnose(
    gold: list[str],
    response_ids: list[str | None],
    entailed_by: list[set[str]],
    retrieved: list[str],
    relevant_chunks: set[str],
    gold_retrieved: set[str],
) -> dict[str, Any]:
    """Compute the RAGChecker-style ratios for one item.

    `response_ids[i]` is a planted claim id or None for an unplanted statement.
    `entailed_by[i]` is the set of retrieved chunk ids that entail that unit.
    """
    if len(response_ids) != len(entailed_by):
        raise ValueError("response and entailment lists differ in length")
    gold_set = set(gold)
    total = len(response_ids)
    precision_num = sum(1 for claim_id in response_ids if claim_id in gold_set)
    if total == 0:
        precision = 0.0
    else:
        precision = precision_num / total
    if not gold_set:
        recall = None
        recall_num = 0
        recall_den = 0
    else:
        present = {claim_id for claim_id in response_ids if claim_id in gold_set}
        recall_num = len(present)
        recall_den = len(gold_set)
        recall = recall_num / recall_den
    if recall is None:
        f1 = None
    else:
        f1 = harmonic(precision, recall)

    faith_num = 0
    rel_num = 0
    irr_num = 0
    hall_num = 0
    self_num = 0
    for claim_id, chunks in zip(response_ids, entailed_by):
        in_gold = claim_id in gold_set
        in_context = bool(chunks)
        if in_context:
            faith_num += 1
        if in_gold and not in_context:
            self_num += 1
        if not in_gold:
            if not in_context:
                hall_num += 1
            elif chunks & relevant_chunks:
                rel_num += 1
            else:
                irr_num += 1

    if not gold_set:
        claim_recall = None
        claim_recall_num = 0
        claim_recall_den = 0
    else:
        claim_recall_num = len(gold_retrieved & gold_set)
        claim_recall_den = len(gold_set)
        claim_recall = claim_recall_num / claim_recall_den

    if not retrieved:
        context_precision = 0.0
        context_precision_num = 0
        context_precision_den = 0
    else:
        context_precision_num = sum(1 for chunk_id in retrieved if chunk_id in relevant_chunks)
        context_precision_den = len(retrieved)
        context_precision = context_precision_num / context_precision_den

    if gold_retrieved:
        used = gold_retrieved & {claim_id for claim_id in response_ids if claim_id in gold_set}
        util_num = len(used)
        util_den = len(gold_retrieved)
        utilization = util_num / util_den
    else:
        util_num = 0
        util_den = 0
        utilization = None

    def rate(numerator: int) -> float | None:
        if total == 0:
            return None
        return numerator / total

    return {
        "precision": precision,
        "precision_num": precision_num,
        "precision_den": total,
        "recall": recall,
        "recall_num": recall_num,
        "recall_den": recall_den,
        "f1": f1,
        "claim_recall": claim_recall,
        "claim_recall_num": claim_recall_num,
        "claim_recall_den": claim_recall_den,
        "context_precision": context_precision,
        "context_precision_num": context_precision_num,
        "context_precision_den": context_precision_den,
        "faithfulness": rate(faith_num),
        "faith_num": faith_num,
        "faith_den": total,
        "relevant_noise": rate(rel_num),
        "relevant_noise_num": rel_num,
        "irrelevant_noise": rate(irr_num),
        "irrelevant_noise_num": irr_num,
        "hallucination": rate(hall_num),
        "hallucination_num": hall_num,
        "self_knowledge": rate(self_num),
        "self_knowledge_num": self_num,
        "context_utilization": utilization,
        "utilization_num": util_num,
        "utilization_den": util_den,
        "response_units": total,
    }


def answer_relevance(question: str, reverse_questions: list[str]) -> float:
    """Logged bag-of-words stand-in for reverse-question cosine. Not a gate."""
    return mean_cosine(question, reverse_questions)


def context_relevance(sentences: list[str], useful_flags: list[bool], insufficient: bool) -> float:
    """Useful sentences over sentences. Zero when the extractor abstains."""
    if insufficient or not sentences:
        return 0.0
    if len(sentences) != len(useful_flags):
        raise ValueError("sentence flags differ in length")
    return sum(1 for flag in useful_flags if flag) / len(sentences)


def split_sentences(text: str) -> list[str]:
    parts = [part.strip() for part in text.replace("?", ".").replace("!", ".").split(".")]
    return [part for part in parts if part]
