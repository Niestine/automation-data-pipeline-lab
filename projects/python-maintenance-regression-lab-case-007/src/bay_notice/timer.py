"""Retransmission estimator with the RFC 6298 update order.

Application profiles may use a floor below one second. They are then more
aggressive than TCP is allowed to be. The SYN-to-3-second rule is not applied.
"""

from __future__ import annotations

from .policy import RetryPolicy


class RtoEstimator:
    def __init__(
        self,
        floor_s: float,
        ceiling_s: float,
        granularity_s: float,
        multiplier: float = 2.0,
        alpha: float = 0.125,
        beta: float = 0.25,
        k: float = 4.0,
        reset_after: int | None = None,
    ) -> None:
        if ceiling_s < floor_s:
            raise ValueError("ceiling must be >= floor")
        self.floor_s = float(floor_s)
        self.ceiling_s = float(ceiling_s)
        self.granularity_s = float(granularity_s)
        self.multiplier = float(multiplier)
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.k = float(k)
        self.reset_after = reset_after
        self.srtt: float | None = None
        self.rttvar: float | None = None
        self.rto = self.floor_s
        self.timeouts = 0

    @classmethod
    def from_policy(cls, policy: RetryPolicy) -> RtoEstimator:
        return cls(
            floor_s=policy.floor_s,
            ceiling_s=policy.ceiling_s,
            granularity_s=policy.granularity_s,
            multiplier=policy.multiplier,
            alpha=policy.alpha,
            beta=policy.beta,
            k=policy.k,
            reset_after=policy.reset_after,
        )

    def observe(self, sample: float, unambiguous: bool) -> bool:
        """Take one RTT sample. Ambiguous samples leave SRTT and RTTVAR alone."""

        if not unambiguous:
            return False
        measurement = float(sample)
        if self.srtt is None or self.rttvar is None:
            self.srtt = measurement
            self.rttvar = measurement / 2.0
        else:
            # RTTVAR uses the previous SRTT. SRTT moves second.
            previous = self.srtt
            self.rttvar = (1.0 - self.beta) * self.rttvar + self.beta * abs(previous - measurement)
            self.srtt = (1.0 - self.alpha) * previous + self.alpha * measurement
        self._recompute()
        self.timeouts = 0
        return True

    def on_timeout(self) -> None:
        doubled = self.rto * self.multiplier
        self.rto = min(self.ceiling_s, max(self.floor_s, doubled))
        self.timeouts += 1
        if self.reset_after is not None and self.timeouts >= self.reset_after:
            self.srtt = None
            self.rttvar = None

    def _recompute(self) -> None:
        assert self.srtt is not None and self.rttvar is not None
        variance_term = self.k * self.rttvar
        extra = self.granularity_s if variance_term == 0 else max(self.granularity_s, variance_term)
        rto = self.srtt + extra
        if rto < self.floor_s:
            rto = self.floor_s
        if rto > self.ceiling_s:
            rto = self.ceiling_s
        self.rto = rto
