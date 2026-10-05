"""Process-wide adaptive retry budget.

The update matches Algorithm 1 in Mehan and Saluja: EMA failure rate,
budget as a fraction of base load, admission probability min(budget, 1 - failure_rate),
an immediate tighten on OVERLOADED, and a hard refuse when the budget is exhausted.
"""

from __future__ import annotations

import random


class ReplayRandom:
    def __init__(self, draws: list[float]) -> None:
        self._draws = list(draws)

    def random(self) -> float:
        if not self._draws:
            raise RuntimeError("admission draw underrun")
        return self._draws.pop(0)


class SharedBudget:
    def __init__(
        self,
        base_load: float,
        seed: int = 0,
        rng: object | None = None,
        initial: float = 0.2,
        ema: float = 0.1,
        alpha: float = 0.1,
        beta: float = 0.5,
        theta_high: float = 0.3,
        theta_low: float = 0.05,
    ) -> None:
        if base_load <= 0:
            raise ValueError("base_load must be positive")
        self.base_load = float(base_load)
        self.initial = float(initial)
        self.budget = float(initial)
        self.ema = float(ema)
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.theta_high = float(theta_high)
        self.theta_low = float(theta_low)
        self.failure_rate = 0.0
        self.last_status = "OK"
        self.rng = rng if rng is not None else random.Random(seed)
        self.admissions = 0
        self.refusals = 0

    def observe(self, failed: bool) -> None:
        indicator = 1.0 if failed else 0.0
        self.failure_rate = (1.0 - self.ema) * self.failure_rate + self.ema * indicator

    def note_status(self, status: str) -> None:
        if status not in {"OK", "OVERLOADED"}:
            raise ValueError(f"unknown status {status!r}")
        self.last_status = status

    def tick(self) -> None:
        """One virtual budget interval. OVERLOADED or a high failure rate tightens."""

        if self.failure_rate > self.theta_high or self.last_status == "OVERLOADED":
            self.budget = self.budget * (1.0 - self.beta)
        elif self.failure_rate < self.theta_low:
            self.budget = min(self.initial, self.budget + self.alpha)

    def admission_probability(self) -> float:
        return min(self.budget, max(0.0, 1.0 - self.failure_rate))

    def try_admit(self) -> bool:
        """Return True when a retry may be sent. A False result must not be retried."""

        if self.budget <= 0 or self.last_status == "OVERLOADED":
            self.refusals += 1
            return False
        probability = self.admission_probability()
        if self.rng.random() < probability:
            self.budget = max(0.0, self.budget - (1.0 / self.base_load))
            self.admissions += 1
            return True
        self.refusals += 1
        return False
