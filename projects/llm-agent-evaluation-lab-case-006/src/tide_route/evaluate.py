"""Cost-quality evaluation: non-decreasing convex hull, AIQ, Zero, and Oracle.

Judge noise flips the external scorer with probability epsilon. The Oracle is
an eval-only ceiling. The Zero router is the hull of the single models.
"""

from __future__ import annotations

import random
from typing import Callable

from tide_route.clock import FakeClock
from tide_route.cost import PriceCard, estimate_cost
from tide_route.provider import FakeProvider, ScriptStep
from tide_route.router import CallerAccount, ModelSpec, RouteRequest, Router, RouterConfig
from tide_route.scorer import Scorer


def nondecreasing_convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop extra spend that does not raise quality, then keep upper-hull vertices."""
    ordered = sorted(points, key=lambda point: (point[0], -point[1]))
    kept: list[tuple[float, float]] = []
    for cost, quality in ordered:
        if kept and abs(cost - kept[-1][0]) <= 1e-12:
            if quality > kept[-1][1] + 1e-12:
                kept[-1] = (cost, quality)
            continue
        if kept and quality <= kept[-1][1] + 1e-12:
            continue
        kept.append((cost, quality))
        while len(kept) >= 3:
            left, mid, right = kept[-3], kept[-2], kept[-1]
            slope_left = (mid[1] - left[1]) / (mid[0] - left[0])
            slope_right = (right[1] - mid[1]) / (right[0] - mid[0])
            if slope_left <= slope_right + 1e-12:
                kept.pop(-2)
            else:
                break
    return kept


def _quality_on(curve: list[tuple[float, float]], cost: float) -> float:
    if cost <= curve[0][0]:
        return curve[0][1]
    if cost >= curve[-1][0]:
        return curve[-1][1]
    for left, right in zip(curve, curve[1:]):
        if left[0] <= cost <= right[0]:
            span = right[0] - left[0]
            if span <= 1e-12:
                return right[1]
            weight = (cost - left[0]) / span
            return left[1] + weight * (right[1] - left[1])
    return curve[-1][1]


def aiq(points: list[tuple[float, float]], c_min: float, c_max: float) -> float:
    """Normalized area under the non-decreasing hull on [c_min, c_max]."""
    if c_max <= c_min:
        return 0.0
    hull = nondecreasing_convex_hull(points)
    if not hull:
        return 0.0
    curve = list(hull)
    if curve[0][0] > c_min + 1e-12:
        curve.insert(0, (c_min, 0.0))
    if curve[-1][0] < c_max - 1e-12:
        curve.append((c_max, curve[-1][1]))
    area = 0.0
    for left, right in zip(curve, curve[1:]):
        lo = max(left[0], c_min)
        hi = min(right[0], c_max)
        if hi <= lo:
            continue
        q_lo = _quality_on(curve, lo)
        q_hi = _quality_on(curve, hi)
        area += (q_lo + q_hi) * 0.5 * (hi - lo)
    return area / (c_max - c_min)


def mix_expectation(
    point_a: tuple[float, float],
    point_b: tuple[float, float],
    probability_a: float,
) -> tuple[float, float]:
    weight = probability_a
    cost = weight * point_a[0] + (1.0 - weight) * point_b[0]
    quality = weight * point_a[1] + (1.0 - weight) * point_b[1]
    return cost, quality


def simulate_mix(rng, point_a, point_b, probability_a: float, draws: int) -> tuple[float, float]:
    costs = []
    qualities = []
    for _ in range(draws):
        chosen = point_a if rng.random() < probability_a else point_b
        costs.append(chosen[0])
        qualities.append(chosen[1])
    return sum(costs) / draws, sum(qualities) / draws


def oracle_point(
    queries: list[dict],
    cost_of: dict[str, float] | Callable[[dict, str], float],
) -> tuple[float, float]:
    """Eval-only ceiling: the cheapest gold-correct model on each query.

    `cost_of` is a flat per-model cost or a function of (query, model_id) so a
    token-priced card is costed on that query's own input length.
    """
    price = cost_of if callable(cost_of) else (lambda _query, model_id: cost_of[model_id])
    costs = []
    qualities = []
    for query in queries:
        correct = [price(query, model_id) for model_id, flag in query["gold"].items() if flag >= 1]
        if not correct:
            costs.append(0.0)
            qualities.append(0.0)
        else:
            costs.append(min(correct))
            qualities.append(1.0)
    count = len(queries) or 1
    return sum(costs) / count, sum(qualities) / count


def slip_text(model_id: str) -> str:
    return (
        '{"berth":"B-1","vessel":"MV '
        + model_id.upper()
        + '","window":"06:00-08:00"}'
    )


def price_card(row: dict) -> PriceCard:
    return PriceCard(
        input_per_token=float(row.get("input_per_token", 0.0)),
        output_per_token=float(row.get("output_per_token", 0.0)),
        per_request=float(row["per_request"]),
    )


def build_router(
    fixture: dict,
    *,
    policy_models: list[str] | None = None,
    lambda_: float,
    epsilon: float,
    seed: int,
    dollars: float | None = None,
    ex_ante_override: dict[str, dict[str, float]] | None = None,
):
    models = []
    scripts = {}
    score_table = {}
    chosen = policy_models or [row["model_id"] for row in fixture["models"]]
    by_id = {row["model_id"]: row for row in fixture["models"]}
    for model_id in chosen:
        row = by_id[model_id]
        ex_ante = {
            query["query_id"]: float(query["gold"][model_id]) for query in fixture["queries"]
        }
        if ex_ante_override and model_id in ex_ante_override:
            ex_ante = dict(ex_ante_override[model_id])
        models.append(
            ModelSpec(
                model_id=model_id,
                provider=row.get("provider", "openai"),
                credential_id=row.get("credential_id", model_id),
                price=price_card(row),
                expected_output_tokens=int(row.get("expected_output_tokens", 0)),
                ex_ante=ex_ante,
            )
        )
        scripts[model_id] = [
            ScriptStep(
                status=200,
                text=slip_text(model_id),
                input_tokens=0,
                output_tokens=0,
                request_id=f"req-{model_id}",
            )
        ]
        for query in fixture["queries"]:
            score_table[(query["query_id"], model_id)] = float(query["gold"][model_id])
    account = CallerAccount(
        caller_id=fixture.get("caller_id", "desk-alpha"),
        dollars=float(fixture["budget"] if dollars is None else dollars),
        request_quota=int(fixture.get("request_quota", 100)),
        token_quota=int(fixture.get("token_quota", 1_000_000)),
        anomaly_rate=int(fixture.get("anomaly_rate", 100)),
    )
    rng = random.Random(seed)
    router = Router(
        models,
        {account.caller_id: account},
        FakeProvider(scripts),
        scorer=Scorer(score_table, epsilon=epsilon, rng=rng),
        clock=FakeClock(),
        rng=rng,
        config=RouterConfig(
            lambda_=lambda_,
            gamma=float(fixture.get("gamma", 0.0)),
            thresholds=(float(fixture.get("threshold", 0.5)),),
            max_depth=int(fixture.get("max_depth", 3)),
            max_attempts=int(fixture.get("max_attempts", 3)),
            max_total_attempts=int(fixture.get("max_total_attempts", 12)),
            max_input_tokens=int(fixture.get("max_input_tokens", 4096)),
            max_input_bytes=int(fixture.get("max_input_bytes", 32768)),
        ),
    )
    return router, account


def run_rows(router: Router, fixture: dict, policy: str, lambda_: float) -> list[dict]:
    rows = []
    caller = fixture.get("caller_id", "desk-alpha")
    for query in fixture["queries"]:
        outcome = router.route(
            RouteRequest(
                caller_id=caller,
                query_id=query["query_id"],
                query_text=query["text"],
                input_tokens=int(query.get("input_tokens", 8)),
                policy=policy,
                lambda_=lambda_,
            )
        )
        quality = 0.0
        if outcome.ok and outcome.model_id:
            quality = float(query["gold"][outcome.model_id])
        rows.append(
            {
                "query_id": query["query_id"],
                "model_id": outcome.model_id,
                "cost": outcome.charged,
                "priced": outcome.priced,
                "quality": quality,
                "http_calls": outcome.http_calls,
                "models_called": list(outcome.models_called),
                "ok": outcome.ok,
                "stop_reason": outcome.stop_reason,
            }
        )
    return rows


def mean_point(rows: list[dict]) -> tuple[float, float]:
    count = len(rows) or 1
    return sum(row["cost"] for row in rows) / count, sum(row["quality"] for row in rows) / count


def _within_caps(rows: list[dict], fixture: dict) -> bool:
    depth = int(fixture.get("max_depth", 3))
    attempts = int(fixture.get("max_total_attempts", 12))
    return all(row["http_calls"] <= attempts and len(row["models_called"]) <= depth for row in rows)


def benchmark(fixture: dict, *, seed: int = 1) -> dict:
    """Run the shipped policies and the epsilon scan on one fixture."""
    lambdas = [float(value) for value in fixture.get("lambdas", [0.5, 5.0])]
    quality_lambda = float(fixture.get("quality_lambda", lambdas[0]))
    c_min = float(fixture["c_min"])
    c_max = float(fixture["c_max"])
    epsilons = [float(value) for value in fixture.get("epsilons", [0.0, 0.1, 0.2, 0.4])]
    rows_by_id = {row["model_id"]: row for row in fixture["models"]}

    def cost_of(query: dict, model_id: str) -> float:
        row = rows_by_id[model_id]
        return estimate_cost(
            price_card(row),
            int(query.get("input_tokens", 8)),
            int(row.get("expected_output_tokens", 0)),
        )

    oracle_cost, oracle_quality = oracle_point(fixture["queries"], cost_of)

    product_points = []
    quality_rows = None
    for index, lambda_ in enumerate(lambdas):
        router, _account = build_router(fixture, lambda_=lambda_, epsilon=0.0, seed=seed + index)
        rows = run_rows(router, fixture, "cascade_routing", lambda_)
        product_points.append(mean_point(rows))
        if abs(lambda_ - quality_lambda) <= 1e-12:
            quality_rows = rows

    ladder_router, _account = build_router(
        fixture, lambda_=quality_lambda, epsilon=0.0, seed=seed + 20
    )
    ladder_rows = run_rows(ladder_router, fixture, "threshold_cascade", quality_lambda)
    ladder_point = mean_point(ladder_rows)

    single_points = []
    for index, row in enumerate(fixture["models"]):
        router, _account = build_router(
            fixture,
            policy_models=[row["model_id"]],
            lambda_=0.0,
            epsilon=0.0,
            seed=seed + 40 + index,
        )
        single_points.append(mean_point(run_rows(router, fixture, "one_shot", 0.0)))

    zero = aiq(single_points, c_min, c_max)
    product = aiq(product_points, c_min, c_max)

    epsilon_rows = []
    crossover = None
    for index, epsilon in enumerate(epsilons):
        points = []
        capped = True
        in_budget = True
        qualities = []
        spent = 0.0
        for lambda_index, lambda_ in enumerate(lambdas):
            router, account = build_router(
                fixture,
                lambda_=lambda_,
                epsilon=epsilon,
                seed=seed + 100 + index * 10 + lambda_index,
            )
            rows = run_rows(router, fixture, "cascade_routing", lambda_)
            points.append(mean_point(rows))
            qualities.append(points[-1][1])
            # Each lambda run has its own caller budget. Compare the priced
            # (unclamped) spend, and treat any clamped charge as a breach.
            run_priced = sum(item["priced"] for item in rows)
            spent += run_priced
            in_budget = (
                in_budget
                and run_priced <= float(fixture["budget"]) + 1e-9
                and not any(item["kind"] == "consumption_spike" for item in router.anomalies)
            )
            capped = capped and _within_caps(rows, fixture)
        score = aiq(points, c_min, c_max)
        epsilon_rows.append(
            {
                "epsilon": epsilon,
                "product_aiq": score,
                "zero_aiq": zero,
                "max_quality": max(qualities) if qualities else 0.0,
                "spent": spent,
                "within_caps": capped,
                "within_budget": in_budget,
            }
        )
        if crossover is None and score <= zero + 1e-12:
            crossover = epsilon

    return {
        "routes": quality_rows or [],
        "product_points": product_points,
        "single_points": single_points,
        "ladder_point": ladder_point,
        "ladder_rows": ladder_rows,
        "product_aiq": product,
        "zero_aiq": zero,
        "oracle_cost": oracle_cost,
        "oracle_quality": oracle_quality,
        "epsilon_rows": epsilon_rows,
        "crossover_epsilon": crossover,
    }
