"""Three-part provider price: output, input, and a per-request fee."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceCard:
    input_per_token: float
    output_per_token: float
    per_request: float


def completion_cost(card: PriceCard, input_tokens: int, output_tokens: int) -> float:
    """Cost of a finished call: c_out * output + c_in * input + c_request."""
    return (
        card.output_per_token * output_tokens
        + card.input_per_token * input_tokens
        + card.per_request
    )


def estimate_cost(card: PriceCard, input_tokens: int, expected_output_tokens: int) -> float:
    """Pre-call estimate. Expected output length is a per-model config value."""
    return completion_cost(card, input_tokens, expected_output_tokens)
