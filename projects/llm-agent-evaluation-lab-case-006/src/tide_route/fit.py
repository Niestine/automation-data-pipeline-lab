"""Validation-split fitting: drop models that never disagree, then a threshold grid."""

from __future__ import annotations


def prune_agreeing(
    answers_by_model: dict[str, list[str]],
    costs: dict[str, float],
) -> list[str]:
    """Keep a model when its answer vector disagrees with every cheaper model kept."""
    order = sorted(answers_by_model, key=lambda model_id: (costs[model_id], model_id))
    kept: list[str] = []
    vectors: dict[str, tuple[str, ...]] = {}
    for model_id in order:
        vector = tuple(answers_by_model[model_id])
        if any(vector == vectors[other] for other in kept):
            continue
        kept.append(model_id)
        vectors[model_id] = vector
    return kept


def fit_threshold(
    records: list[dict],
    grid: list[float],
    budget: float,
) -> dict | None:
    """Pick the grid threshold with the best accuracy whose mean cost is within budget.

    Each record has `steps` in cheap-to-expensive order. A step carries cost,
    score, and correct. The cascade stops at the first score that meets the
    threshold and otherwise pays for every step.
    """
    best: dict | None = None
    for threshold in grid:
        costs: list[float] = []
        correct: list[float] = []
        for record in records:
            spent = 0.0
            hit = False
            ok = False
            for step in record["steps"]:
                spent += float(step["cost"])
                if float(step["score"]) >= threshold:
                    ok = bool(step["correct"])
                    hit = True
                    break
            if not hit and record["steps"]:
                ok = bool(record["steps"][-1]["correct"])
            costs.append(spent)
            correct.append(1.0 if ok else 0.0)
        mean_cost = sum(costs) / len(costs) if costs else 0.0
        mean_quality = sum(correct) / len(correct) if correct else 0.0
        if mean_cost <= budget + 1e-9:
            candidate = {
                "threshold": threshold,
                "mean_cost": mean_cost,
                "mean_quality": mean_quality,
            }
            if best is None or (
                candidate["mean_quality"],
                -candidate["mean_cost"],
            ) > (best["mean_quality"], -best["mean_cost"]):
                best = candidate
    return best
