"""Token-bucket rate limiter with a minimum interval between fetches.

Crawl-delay from robots.txt becomes `min_interval_ms`. Burst > 1 allows a
short front-load and then settles to one token per interval. Interval 0
disables waiting so unit tests that do not care about politeness stay fast.
"""

from __future__ import annotations

from typing import Any, Optional
import math

from .telemetry import JsonLogger, WallClock


class RateLimiter:
    def __init__(
        self,
        *,
        min_interval_ms: int = 0,
        burst: int = 1,
        clock: Any = None,
        sleeper: Any = None,
        logger: Optional[JsonLogger] = None,
    ) -> None:
        self.min_interval_ms = max(0, int(min_interval_ms))
        self.burst = max(1, int(burst))
        self.tokens = float(self.burst)
        self.clock = clock if clock is not None else WallClock()
        self.sleeper = sleeper if sleeper is not None else self.clock.sleep
        self.logger = logger
        self.last_refill_ms = self.clock.now_ms()
        self.last_request_ms: Optional[int] = None
        self.total_wait_ms = 0
        self.acquire_count = 0

    def set_min_interval_ms(self, value: int) -> None:
        self._refill()
        self.min_interval_ms = max(0, int(value))
        if self.last_request_ms is not None and self.min_interval_ms > 0:
            elapsed = self.clock.now_ms() - self.last_request_ms
            self.tokens = min(float(self.burst), max(0.0, elapsed / self.min_interval_ms))
            self.last_refill_ms = self.clock.now_ms()

    def wait(self) -> int:
        """Block until a fetch is allowed. Returns milliseconds actually waited."""
        started = self.clock.now_ms()
        self._refill()
        delay = 0
        if self.tokens < 1:
            if self.min_interval_ms <= 0:
                self.tokens = float(self.burst)
            else:
                missing = 1.0 - self.tokens
                # Round up so a fractional token never admits a fetch early.
                delay = math.ceil(missing * self.min_interval_ms)
        if delay > 0:
            if self.logger is not None:
                self.logger.log("rate_limit_wait", waited_ms=delay, tokens=round(self.tokens, 3))
            self.sleeper(delay)
            self._refill()
            self.tokens = max(self.tokens, 1.0)

        self.tokens = max(0.0, self.tokens - 1.0)
        now = self.clock.now_ms()
        self.last_request_ms = now
        waited = now - started
        self.total_wait_ms += waited
        self.acquire_count += 1
        return waited

    def _refill(self) -> None:
        now = self.clock.now_ms()
        if self.min_interval_ms <= 0:
            self.tokens = float(self.burst)
            self.last_refill_ms = now
            return
        elapsed = now - self.last_refill_ms
        if elapsed <= 0:
            return
        self.tokens = min(float(self.burst), self.tokens + elapsed / self.min_interval_ms)
        self.last_refill_ms = now
