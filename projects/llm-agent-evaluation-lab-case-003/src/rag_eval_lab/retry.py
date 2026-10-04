"""Fixed-budget backoff for schema failures. Policy rejects are not retried."""

from __future__ import annotations

import random


class RetryPolicy:
    def __init__(self, attempts: int = 3, base_seconds: float = 0.05) -> None:
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        self.attempts = attempts
        self.base_seconds = base_seconds


def backoff_seconds(attempt: int, seed: int, item_id: str, base: float = 0.05) -> float:
    """Exponential delay for a failed attempt index, jittered by seed and item id."""
    span = base * (2**attempt)
    rng = random.Random(f"{seed}:{item_id}:{attempt}")
    return round(span + rng.random() * base * 0.2, 6)
