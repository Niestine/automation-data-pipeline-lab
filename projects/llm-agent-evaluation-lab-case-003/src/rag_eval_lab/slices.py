"""RGB noise, rejection, integration, and counterfactual probes."""

from __future__ import annotations

from typing import Any

from .errors import LabInputError
from .models import Corpus, Item
from .retrieve import Hit
from .scoring import contract_errors, error_detection, materialize_payload, resolve_claims


NOISE_RATIOS = (0.0, 0.2, 0.4, 0.6, 0.8)


def mix_ids(positives: list[str], negatives: list[str], ratio: float, width: int = 5) -> list[str]:
    negative_count = int(round(ratio * width))
    positive_count = width - negative_count
    if positive_count > len(positives) or negative_count > len(negatives):
        raise ValueError("not enough chunks for the noise mix")
    return list(positives[:positive_count]) + list(negatives[:negative_count])


def hits_from_ids(corpus: Corpus, chunk_ids: list[str]) -> list[Hit]:
    hits: list[Hit] = []
    for chunk_id in chunk_ids:
        chunk = corpus.chunk(chunk_id)
        hits.append(Hit(chunk.chunk_id, 1.0, chunk.text, chunk.tags))
    return hits


def ready_payload(corpus: Corpus, item: Item, payload: dict[str, Any]) -> dict[str, Any]:
    materialized, pre_errors = materialize_payload(payload, corpus)
    errors = list(pre_errors) + contract_errors(materialized, corpus, item.item_id)
    if errors:
        raise LabInputError("; ".join(errors))
    return materialized


def run_noise_curve(
    corpus: Corpus,
    item: Item,
    scripts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Five-document contexts. Topical negatives replace positives as the ratio rises."""
    points = []
    for ratio in NOISE_RATIOS:
        chunk_ids = mix_ids(list(item.positive_chunk_ids), list(item.negative_chunk_ids), ratio)
        payload = ready_payload(corpus, item, scripts[f"{ratio:.1f}"])
        result = resolve_claims(corpus, item, payload, hits_from_ids(corpus, chunk_ids))
        points.append(
            {
                "ratio": ratio,
                "accuracy": 1.0 if result["accurate"] else 0.0,
                "claim_recall": result["metrics"]["claim_recall"],
                "chunk_ids": chunk_ids,
            }
        )
    return {"points": points, "outside_memory": item.outside_memory}


def run_counterfactual(corpus: Corpus, item: Item, payload: dict[str, Any]) -> dict[str, Any]:
    hits = hits_from_ids(corpus, list(item.forced_chunk_ids))
    result = resolve_claims(corpus, item, ready_payload(corpus, item, payload), hits)
    rates = error_detection(result["rows"], set(item.false_claim_ids), set(item.gold_claim_ids))
    rates["guardrail_blocked"] = bool(result["fail_closed"] and result["fail_reason"] == "counterfactual")
    rates["acceptance"] = result["acceptance"]
    rates["accurate"] = result["accurate"]
    return rates
