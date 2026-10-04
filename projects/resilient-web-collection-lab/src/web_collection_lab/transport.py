"""HTTP-shaped request/response types and transports.

The default transport is in-process: it never opens a socket. That keeps
the lab offline and deterministic while still exercising header, status,
and body contracts a real HTTP client would see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from urllib.parse import urlencode

from .errors import HttpError, TransportError
from .retry import (
    RetryPolicy,
    backoff_ms,
    delay_from_headers,
    is_retryable_error,
    is_retryable_request,
    is_retryable_status,
    rng_for,
)
from .telemetry import JsonLogger, WallClock


def normalize_headers(headers: Optional[dict[str, str]]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in (headers or {}).items()}


@dataclass
class HttpRequest:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def __post_init__(self) -> None:
        self.method = self.method.upper()
        self.headers = normalize_headers(self.headers)
        self.query = {str(k): str(v) for k, v in (self.query or {}).items()}
        if not isinstance(self.body, (bytes, bytearray)):
            raise TypeError("body must be bytes")
        self.body = bytes(self.body)

    @property
    def url(self) -> str:
        if not self.query:
            return self.path
        return f"{self.path}?{urlencode(sorted(self.query.items()))}"


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def __post_init__(self) -> None:
        self.headers = normalize_headers(self.headers)
        self.body = bytes(self.body)

    @property
    def request_id(self) -> Optional[str]:
        return self.headers.get("x-request-id")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type") or ""

    @property
    def etag(self) -> Optional[str]:
        return self.headers.get("etag")


class Transport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class InProcessTransport:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.calls.append(request)
        return self.handler.handle(request)


class RetryingTransport:
    """Wraps a transport with status/timeout retries and backoff.

    When a limiter is supplied, every retry also waits for it, so a
    `Retry-After: 0` cannot bypass robots.txt Crawl-delay.
    """

    def __init__(
        self,
        inner: Transport,
        *,
        policy: Optional[RetryPolicy] = None,
        logger: Optional[JsonLogger] = None,
        clock: Any = None,
        sleeper: Any = None,
        seed: int = 7,
        limiter: Any = None,
    ) -> None:
        self.inner = inner
        self.limiter = limiter
        self.policy = policy if policy is not None else RetryPolicy()
        self.logger = logger if logger is not None else JsonLogger()
        self.clock = clock if clock is not None else WallClock()
        self.sleeper = sleeper if sleeper is not None else self.clock.sleep
        self.seed = seed
        self.retry_count = 0

    def send(self, request: HttpRequest) -> HttpResponse:
        """Return the first non-retryable response; re-raise the final transport error."""
        attempts = max(1, self.policy.max_attempts)
        for attempt in range(1, attempts + 1):
            self.logger.log(
                "request_start",
                method=request.method,
                path=request.path,
                attempt=attempt,
                url=request.url,
            )
            try:
                response = self.inner.send(request)
            except TransportError as exc:
                retryable = (
                    is_retryable_error(exc)
                    and is_retryable_request(request.method, request.headers)
                    and attempt < attempts
                )
                self.logger.log(
                    "request_error",
                    method=request.method,
                    path=request.path,
                    attempt=attempt,
                    code=exc.code,
                    retryable=retryable,
                )
                if not retryable:
                    raise
                self._sleep_backoff(request, attempt, headers={})
                self._wait_limiter()
                continue

            if response.status < 400:
                self.logger.log(
                    "request_success",
                    method=request.method,
                    path=request.path,
                    attempt=attempt,
                    status=response.status,
                    request_id=response.request_id,
                )
                return response

            retryable = (
                is_retryable_status(response.status)
                and is_retryable_request(request.method, request.headers)
                and attempt < attempts
            )
            self.logger.log(
                "request_failure",
                method=request.method,
                path=request.path,
                attempt=attempt,
                status=response.status,
                retryable=retryable,
                request_id=response.request_id,
            )
            if not retryable:
                return response
            self._sleep_backoff(request, attempt, headers=response.headers)
            self._wait_limiter()

        raise AssertionError("unreachable: final attempt is never retried")

    def _wait_limiter(self) -> None:
        if self.limiter is not None:
            self.limiter.wait()

    def _sleep_backoff(self, request: HttpRequest, attempt: int, headers: dict[str, str]) -> None:
        self.retry_count += 1
        header_delay = delay_from_headers(headers)
        if header_delay is not None:
            delay = min(header_delay, self.policy.max_retry_after_ms)
        else:
            rng = rng_for(self.seed, attempt, request.method, request.path, request.url)
            delay = backoff_ms(self.policy, attempt, rng)
        self.logger.log(
            "request_retry",
            method=request.method,
            path=request.path,
            attempt=attempt,
            delay_ms=delay,
        )
        self.sleeper(delay)


def raise_for_status(response: HttpResponse) -> None:
    if response.status < 400:
        return
    code = "http_error"
    message = f"HTTP {response.status}"
    raise HttpError(
        code,
        message,
        status=response.status,
        retryable=is_retryable_status(response.status),
        request_id=response.request_id,
    )
