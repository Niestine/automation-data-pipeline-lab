"""Polite GET client: rate limit, User-Agent, conditional requests."""

from __future__ import annotations

from typing import Any, Optional

from .models import USER_AGENT
from .rate_limit import RateLimiter
from .retry import RetryPolicy
from .telemetry import JsonLogger, WallClock
from .transport import (
    HttpRequest,
    HttpResponse,
    InProcessTransport,
    RetryingTransport,
    raise_for_status,
)


class SiteClient:
    def __init__(
        self,
        transport: Any,
        *,
        limiter: RateLimiter,
        user_agent: str = USER_AGENT,
        logger: Optional[JsonLogger] = None,
        force: bool = False,
    ) -> None:
        self.transport = transport
        self.limiter = limiter
        self.user_agent = user_agent
        self.logger = logger if logger is not None else JsonLogger()
        self.force = force

    def get(
        self,
        path: str,
        *,
        query: Optional[dict[str, str]] = None,
        etag: Optional[str] = None,
    ) -> HttpResponse:
        waited = self.limiter.wait()
        headers = {
            "user-agent": self.user_agent,
            "accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "accept-language": "en",
        }
        if etag and not self.force:
            headers["if-none-match"] = etag
        request = HttpRequest("GET", path, query=query or {}, headers=headers)
        response = self.transport.send(request)
        if response.status == 304:
            self.logger.log(
                "not_modified",
                path=path,
                etag=etag,
                waited_ms=waited,
                request_id=response.request_id,
            )
            return response
        if response.status >= 400:
            raise_for_status(response)
        return response


def build_client(
    site: Any,
    *,
    policy: Optional[RetryPolicy] = None,
    logger: Optional[JsonLogger] = None,
    clock: Any = None,
    sleeper: Any = None,
    seed: int = 7,
    min_interval_ms: int = 0,
    burst: int = 1,
    user_agent: str = USER_AGENT,
    force: bool = False,
) -> tuple[SiteClient, RetryingTransport, RateLimiter, InProcessTransport]:
    logger = logger if logger is not None else JsonLogger()
    clock = clock if clock is not None else WallClock()
    sleeper = sleeper if sleeper is not None else clock.sleep
    inner = InProcessTransport(site)
    limiter = RateLimiter(
        min_interval_ms=min_interval_ms,
        burst=burst,
        clock=clock,
        sleeper=sleeper,
        logger=logger,
    )
    retrying = RetryingTransport(
        inner,
        policy=policy if policy is not None else RetryPolicy(),
        logger=logger,
        clock=clock,
        sleeper=sleeper,
        seed=seed,
        limiter=limiter,
    )
    client = SiteClient(
        retrying,
        limiter=limiter,
        user_agent=user_agent,
        logger=logger,
        force=force,
    )
    return client, retrying, limiter, inner
