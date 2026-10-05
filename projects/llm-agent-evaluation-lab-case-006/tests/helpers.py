"""Shared constructors for the tide-desk tests."""

from __future__ import annotations

import random
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SRC = PROJECT / "src"
EXAMPLES = PROJECT / "examples"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tide_route.clock import FakeClock  # noqa: E402
from tide_route.cost import PriceCard  # noqa: E402
from tide_route.provider import SLIP_OK, FakeProvider, ScriptStep  # noqa: E402
from tide_route.router import (  # noqa: E402
    CallerAccount,
    ModelSpec,
    RouteRequest,
    Router,
    RouterConfig,
)
from tide_route.scorer import Scorer  # noqa: E402


class SeqRNG:
    """Fixed random() sequence. A Random subclass hashes its constructor argument."""

    def __init__(self, values) -> None:
        self.values = list(values)

    def random(self):  # noqa: A003
        if self.values:
            return self.values.pop(0)
        return 0.0


def card(per_request=0.0, input_per_token=0.0, output_per_token=0.0) -> PriceCard:
    return PriceCard(input_per_token, output_per_token, per_request)


def model(
    model_id,
    *,
    provider="openai",
    credential=None,
    per_request=0.0,
    input_per_token=0.0,
    output_per_token=0.0,
    expected_output_tokens=0,
    ex_ante=None,
    default_quality=0.5,
) -> ModelSpec:
    return ModelSpec(
        model_id=model_id,
        provider=provider,
        credential_id=credential or model_id,
        price=card(per_request, input_per_token, output_per_token),
        expected_output_tokens=expected_output_tokens,
        ex_ante=dict(ex_ante or {}),
        default_quality=default_quality,
    )


def make_router(
    models,
    scripts=None,
    *,
    dollars=10.0,
    caller_id="desk-alpha",
    extra_accounts=None,
    lambda_=0.1,
    gamma=0.0,
    threshold=0.5,
    thresholds=None,
    max_depth=3,
    max_attempts=3,
    max_total_attempts=20,
    max_input_tokens=1000,
    max_input_bytes=10000,
    epsilon=0.0,
    scores=None,
    bucket_balance=500,
    seed=0,
    rng=None,
    anomaly_rate=100,
    request_quota=100,
    token_quota=1_000_000,
    anomaly_spend=1_000_000.0,
    attempt_timeout_s=30.0,
):
    from tide_route.bucket import RetryBucket

    prepared = {}
    for spec in models:
        prepared[spec.model_id] = [ScriptStep()]
    if scripts:
        prepared.update(scripts)
    rng = rng or random.Random(seed)
    account = CallerAccount(
        caller_id=caller_id,
        dollars=dollars,
        request_quota=request_quota,
        token_quota=token_quota,
        anomaly_rate=anomaly_rate,
    )
    accounts = {caller_id: account}
    if extra_accounts:
        accounts.update(extra_accounts)
    router = Router(
        list(models),
        accounts,
        FakeProvider(prepared),
        scorer=Scorer(scores or {}, epsilon=epsilon, rng=rng, default=1.0),
        clock=FakeClock(),
        rng=rng,
        bucket=RetryBucket(balance=bucket_balance),
        config=RouterConfig(
            lambda_=lambda_,
            gamma=gamma,
            thresholds=thresholds or (threshold,),
            max_depth=max_depth,
            max_attempts=max_attempts,
            max_total_attempts=max_total_attempts,
            max_input_tokens=max_input_tokens,
            max_input_bytes=max_input_bytes,
            attempt_timeout_s=attempt_timeout_s,
            anomaly_spend=anomaly_spend,
        ),
    )
    return router


def request(query_id="Q", text="notice for MV EXAMPLE", tokens=8, caller="desk-alpha", **kwargs):
    return RouteRequest(
        caller_id=caller,
        query_id=query_id,
        query_text=text,
        input_tokens=tokens,
        **kwargs,
    )
