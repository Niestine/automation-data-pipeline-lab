"""Cascade routing: tau = quality_hat - lambda * cost_hat over supermodels.

After each answered call the same rule is applied to supermodels that contain
every model already used. The cheapest unused member of the chosen supermodel
is the next call. A model with a strictly negative marginal tau is removed
together with every supermodel that contains that set. Ties draw the cheapest
tied supermodel with probability gamma and the costliest otherwise.
"""

from __future__ import annotations

import itertools


def tau(quality: float, cost: float, lambda_: float) -> float:
    return quality - lambda_ * cost


def _subsets(model_ids: list[str], already: set[str]) -> list[tuple[str, ...]]:
    rest = [model_id for model_id in model_ids if model_id not in already]
    base = tuple(sorted(already))
    found: list[tuple[str, ...]] = []
    for width in range(len(rest) + 1):
        for combo in itertools.combinations(rest, width):
            if not base and not combo:
                continue
            found.append(tuple(sorted((*base, *combo))))
    return found


def _quality_of(members: tuple[str, ...] | set[str], qualities: dict[str, float]) -> float:
    if not members:
        return float("-inf")
    return max(qualities[member] for member in members)


def _cost_of(members: tuple[str, ...] | set[str], costs: dict[str, float]) -> float:
    return sum(costs[member] for member in members)


def pruned_supermodels(
    model_ids: list[str],
    qualities: dict[str, float],
    costs: dict[str, float],
    lambda_: float,
    already: set[str],
) -> set[tuple[str, ...]]:
    """Supermodels removed by a strictly negative marginal gain."""
    candidates = _subsets(model_ids, already)
    present = set(candidates)
    pruned: set[tuple[str, ...]] = set()
    for members in candidates:
        member_set = set(members)
        for model_id in members:
            rest = member_set - {model_id}
            if not rest or not already.issubset(rest):
                continue
            rest_key = tuple(sorted(rest))
            if rest_key not in present:
                continue
            margin = tau(
                _quality_of(members, qualities),
                _cost_of(members, costs),
                lambda_,
            ) - tau(
                _quality_of(rest_key, qualities),
                _cost_of(rest_key, costs),
                lambda_,
            )
            if margin < -1e-12:
                for other in candidates:
                    if member_set.issubset(other):
                        pruned.add(other)
    return pruned


def choose_supermodel(
    model_ids: list[str],
    qualities: dict[str, float],
    costs: dict[str, float],
    already: set[str],
    lambda_: float,
    gamma: float,
    rng,
) -> tuple[str, ...] | None:
    pruned = pruned_supermodels(model_ids, qualities, costs, lambda_, already)
    candidates = [
        members
        for members in _subsets(model_ids, already)
        if members not in pruned
    ]
    if not candidates:
        return None
    scored = [
        (
            tau(_quality_of(members, qualities), _cost_of(members, costs), lambda_),
            _cost_of(members, costs),
            members,
        )
        for members in candidates
    ]
    best = max(row[0] for row in scored)
    tied = [row for row in scored if abs(row[0] - best) <= 1e-9]
    if len(tied) == 1:
        return tied[0][2]
    cheapest = min(tied, key=lambda row: (row[1], row[2]))
    costliest = max(tied, key=lambda row: (row[1], row[2]))
    if gamma <= 0.0:
        return costliest[2]
    if gamma >= 1.0:
        return cheapest[2]
    if rng.random() < gamma:
        return cheapest[2]
    return costliest[2]


def next_model(
    model_ids: list[str],
    qualities: dict[str, float],
    costs: dict[str, float],
    already: set[str],
    lambda_: float,
    gamma: float,
    rng,
) -> str | None:
    """Cheapest unused model of the chosen supermodel, or None to stop."""
    chosen = choose_supermodel(
        model_ids, qualities, costs, already, lambda_, gamma, rng
    )
    if chosen is None:
        return None
    unused = [model_id for model_id in chosen if model_id not in already]
    if not unused:
        return None
    return min(unused, key=lambda model_id: (costs[model_id], model_id))


def select_one(
    model_ids: list[str],
    qualities: dict[str, float],
    costs: dict[str, float],
    lambda_: float,
    gamma: float,
    rng,
) -> str | None:
    """One-shot route: tau over individual models, with the same gamma tie break."""
    if not model_ids:
        return None
    scored = [
        (tau(qualities[model_id], costs[model_id], lambda_), costs[model_id], model_id)
        for model_id in model_ids
    ]
    best = max(row[0] for row in scored)
    tied = [row for row in scored if abs(row[0] - best) <= 1e-9]
    if len(tied) == 1:
        return tied[0][2]
    cheapest = min(tied, key=lambda row: (row[1], row[2]))
    costliest = max(tied, key=lambda row: (row[1], row[2]))
    if gamma <= 0.0:
        return costliest[2]
    if gamma >= 1.0:
        return cheapest[2]
    if rng.random() < gamma:
        return cheapest[2]
    return costliest[2]


def ordered_by_cost(model_ids: list[str], costs: dict[str, float]) -> list[str]:
    """Cheap-to-expensive ladder used only as an evaluation baseline."""
    return sorted(model_ids, key=lambda model_id: (costs[model_id], model_id))
