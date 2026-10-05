"""External scorer. With probability epsilon, g is replaced by 1 - g."""

from __future__ import annotations

import random


class Scorer:
    def __init__(
        self,
        table: dict[tuple[str, str], float] | None = None,
        *,
        epsilon: float = 0.0,
        rng: random.Random | None = None,
        default: float = 1.0,
    ) -> None:
        self.table = dict(table or {})
        self.epsilon = epsilon
        self.rng = rng or random.Random(0)
        self.default = default

    def score(self, query_id: str, model_id: str, answer: str) -> float:
        del answer  # the fixture table is the scorer; the text is not parsed
        found = self.table.get((query_id, model_id), self.default)
        if self.epsilon > 0.0 and self.rng.random() < self.epsilon:
            return 1.0 - found
        return found
