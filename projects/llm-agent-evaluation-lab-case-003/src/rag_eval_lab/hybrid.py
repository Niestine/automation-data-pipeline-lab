"""Hybrid text-weight sweep. Pooled optimum and per-label optimum are both reported."""

from __future__ import annotations

from typing import Any


def hybrid_score(lexical: float, dense: float, text_weight: float) -> float:
    return (1.0 - text_weight) * dense + text_weight * lexical


def best_text_weights(
    examples: list[dict[str, Any]],
    weights: list[float],
    floor: float,
) -> dict[str, Any]:
    """Claim recall is 1 when the single positive chunk clears the floor."""
    by_label: dict[str, dict[str, float]] = {}
    pooled: dict[str, float] = {}
    details: list[dict[str, Any]] = []
    for weight in weights:
        key = f"{weight:.2f}"
        hits = []
        per_label_hits: dict[str, list[int]] = {}
        for example in examples:
            score = hybrid_score(float(example["lexical"]), float(example["dense"]), weight)
            hit = 1 if score >= floor else 0
            label = str(example["label"])
            per_label_hits.setdefault(label, []).append(hit)
            hits.append(hit)
            details.append({"label": label, "text_weight": weight, "score": score, "hit": hit})
        pooled[key] = sum(hits) / len(hits) if hits else 0.0
        for label, values in per_label_hits.items():
            by_label.setdefault(label, {})[key] = sum(values) / len(values)

    def best_key(scores: dict[str, float]) -> str:
        return max(scores, key=lambda item: (scores[item], -float(item)))

    pooled_best = best_key(pooled)
    reasoning_best = best_key(by_label["reasoning"])
    return {
        "pooled": pooled,
        "by_label": by_label,
        "pooled_best_text_weight": float(pooled_best),
        "reasoning_best_text_weight": float(reasoning_best),
        "optima_differ": float(pooled_best) != float(reasoning_best),
        "floor": floor,
        "weights": weights,
    }
