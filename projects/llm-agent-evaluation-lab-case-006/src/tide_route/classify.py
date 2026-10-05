"""Vendor error code wins over the HTTP status class.

OpenAI billing codes are terminal even when error.type is insufficient_quota.
A Claude 429 with no retry-after is ambiguous in one response: the lab allows
one bounded backoff, and a second consecutive bare 429 on that credential
becomes terminal billing. That escalation is not applied to OpenAI.

Integer Retry-After is the only delay form this lab parses. An HTTP-date is
left unparsed because the archived RFC 9110 windows do not contain that ABNF.
"""

from __future__ import annotations

from dataclasses import dataclass

OPENAI_BILLING = frozenset(
    {
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
)

AWS_TIMEOUT_CODES = frozenset({"RequestTimeout", "RequestTimeoutException"})
AWS_VALIDATION_CODES = frozenset({"ValidationException"})
AWS_THROTTLE_CODES = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "Throttling",
        "SlowDown",
        "ProvisionedThroughputExceededException",
    }
)


@dataclass(frozen=True)
class Decision:
    failure_class: str
    retryable: bool
    failover_allowed: bool
    retry_kind: str
    problem_slug: str
    http_status: int
    blocks: str
    retry_after_seconds: int | None
    retry_after_parse: str
    lab_rule: str = ""


def parse_retry_after(headers: dict | None) -> tuple[int | None, str]:
    """Return (seconds, 'integer'|'absent_or_unparsed'). Dates are not parsed."""
    if not headers:
        return None, "absent_or_unparsed"
    raw = None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None, "absent_or_unparsed"
    text = str(raw).strip()
    if text.isascii() and text.isdigit():
        return int(text), "integer"
    return None, "absent_or_unparsed"


def _decision(
    failure_class: str,
    *,
    retryable: bool,
    failover_allowed: bool,
    retry_kind: str,
    problem_slug: str,
    http_status: int,
    blocks: str,
    retry_after_seconds: int | None,
    retry_after_parse: str,
    lab_rule: str = "",
) -> Decision:
    return Decision(
        failure_class=failure_class,
        retryable=retryable,
        failover_allowed=failover_allowed,
        retry_kind=retry_kind if retryable else "",
        problem_slug=problem_slug,
        http_status=http_status,
        blocks=blocks,
        retry_after_seconds=retry_after_seconds,
        retry_after_parse=retry_after_parse,
        lab_rule=lab_rule,
    )


def classify(
    *,
    vendor: str,
    status: int,
    error_code: str | None = None,
    error_type: str | None = None,
    headers: dict | None = None,
    timeout: bool = False,
    network: bool = False,
    sse_error: bool = False,
    spend_limit: bool = False,
    bare_429_seen: bool = False,
    message: str | None = None,
) -> Decision:
    """Classify one provider failure. `message` is accepted and ignored."""
    del message  # detail prose is not a lookup key
    seconds, parsed = parse_retry_after(headers)
    code = error_code or ""
    etype = error_type or ""
    vendor_name = (vendor or "").lower()

    if timeout or etype == "timeout_error" or status == 504 or etype == "APITimeoutError":
        return _decision(
            "timeout",
            retryable=False,
            failover_allowed=True,
            retry_kind="",
            problem_slug="timeout",
            http_status=504,
            blocks="",
            retry_after_seconds=seconds,
            retry_after_parse=parsed,
        )
    if network or status == 511 or etype == "APIConnectionError":
        return _decision(
            "network",
            retryable=True,
            failover_allowed=True,
            retry_kind="transient",
            problem_slug="network",
            http_status=511 if status == 511 else 503,
            blocks="",
            retry_after_seconds=seconds,
            retry_after_parse=parsed,
        )
    if sse_error:
        return _decision(
            "sse_error",
            retryable=False,
            failover_allowed=True,
            retry_kind="",
            problem_slug="unavailable",
            http_status=502,
            blocks="",
            retry_after_seconds=None,
            retry_after_parse="absent_or_unparsed",
        )

    if vendor_name == "aws":
        if code in AWS_TIMEOUT_CODES:
            return _retry("transient", "unavailable", status or 400, seconds, parsed, failover=True)
        if code in AWS_VALIDATION_CODES:
            return _defect(status or 400, seconds, parsed)
        if code in AWS_THROTTLE_CODES:
            return _retry("throttling", "rate-limited", status or 429, seconds, parsed, failover=True)

    if vendor_name == "openai":
        if code in OPENAI_BILLING:
            return _billing(seconds, parsed)
        if code == "service_tier":
            return _defect(400, seconds, parsed)
        if code == "slow_down" or (status == 429 and etype == "rate_limit_error"):
            return _retry("throttling", "rate-limited", 429, seconds, parsed, failover=False)
        if status == 429 and code not in OPENAI_BILLING:
            return _retry("throttling", "rate-limited", 429, seconds, parsed, failover=False)
        if code == "server_is_overloaded" or status == 503 or etype == "service_unavailable_error":
            return _retry("throttling", "overloaded", 503, seconds, parsed, failover=True, failure_class="overloaded")
        if status == 500 or etype == "InternalServerError":
            return _retry("transient", "unavailable", 500, seconds, parsed, failover=True)
        if status in (401, 403):
            return _auth(status, "credential", seconds, parsed)
        if status == 404:
            return _auth(404, "model", seconds, parsed)
        if etype == "RateLimitError":
            return _retry("throttling", "rate-limited", 429, seconds, parsed, failover=False)
        if status == 400:
            return _defect(400, seconds, parsed)

    if vendor_name == "anthropic":
        if status == 402 or etype == "billing_error":
            return _billing(seconds, parsed)
        if status == 400 and spend_limit:
            return _billing(seconds, parsed, lab_rule="spend_limit_400")
        if status == 413 or etype == "request_too_large" or status == 431:
            return _defect(status, seconds, parsed)
        if status == 400:
            return _defect(400, seconds, parsed)
        if status == 409 or etype == "conflict_error":
            return _decision(
                "conflict",
                retryable=False,
                failover_allowed=False,
                retry_kind="",
                problem_slug="conflict",
                http_status=409,
                blocks="all",
                retry_after_seconds=seconds,
                retry_after_parse=parsed,
            )
        if status == 429 or etype == "rate_limit_error":
            if parsed != "integer":
                if bare_429_seen:
                    return _billing(
                        seconds,
                        parsed,
                        lab_rule="second_bare_429",
                    )
                return _retry(
                    "throttling",
                    "rate-limited",
                    429,
                    seconds,
                    parsed,
                    failover=False,
                    lab_rule="first_bare_429",
                )
            return _retry("throttling", "rate-limited", 429, seconds, parsed, failover=False)
        if status == 529 or etype == "overloaded_error":
            return _retry(
                "throttling",
                "overloaded",
                503,
                seconds,
                parsed,
                failover=True,
                failure_class="overloaded",
            )
        if status == 500 or etype == "api_error":
            return _retry("transient", "unavailable", 500, seconds, parsed, failover=True)
        if status in (401, 403):
            return _auth(status, "credential", seconds, parsed)
        if status == 404:
            return _auth(404, "model", seconds, parsed)

    if not code and not etype:
        # Unrecognized codes follow the generic class and are not retried.
        if status == 499 or (400 <= status <= 499 and status != 429):
            return _defect(400 if status == 499 else status, seconds, parsed)
        if status == 429:
            return _retry("throttling", "rate-limited", 429, seconds, parsed, failover=False)
        if 500 <= status <= 599:
            return _retry("transient", "unavailable", status, seconds, parsed, failover=True)

    if 500 <= status <= 599:
        return _retry("transient", "unavailable", status, seconds, parsed, failover=True)
    return _defect(status or 400, seconds, parsed)


def _billing(
    seconds: int | None,
    parsed: str,
    lab_rule: str = "",
) -> Decision:
    return _decision(
        "terminal_billing",
        retryable=False,
        failover_allowed=False,
        retry_kind="",
        problem_slug="terminal-billing",
        http_status=402,
        blocks="credential",
        retry_after_seconds=seconds,
        retry_after_parse=parsed,
        lab_rule=lab_rule,
    )


def _defect(http_status: int, seconds: int | None, parsed: str) -> Decision:
    return _decision(
        "request_defect",
        retryable=False,
        failover_allowed=False,
        retry_kind="",
        problem_slug="request-defect",
        http_status=http_status if 400 <= http_status <= 499 else 400,
        blocks="all",
        retry_after_seconds=seconds,
        retry_after_parse=parsed,
    )


def _auth(http_status: int, blocks: str, seconds: int | None, parsed: str) -> Decision:
    return _decision(
        "unauthorized",
        retryable=False,
        failover_allowed=blocks != "credential",
        retry_kind="",
        problem_slug="unauthorized",
        http_status=http_status,
        blocks=blocks,
        retry_after_seconds=seconds,
        retry_after_parse=parsed,
    )


def _retry(
    kind: str,
    slug: str,
    http_status: int,
    seconds: int | None,
    parsed: str,
    *,
    failover: bool,
    failure_class: str | None = None,
    lab_rule: str = "",
) -> Decision:
    if failure_class is None:
        failure_class = {
            "rate-limited": "rate_limited",
            "overloaded": "overloaded",
            "unavailable": "transient",
        }.get(slug, slug.replace("-", "_"))
    # "hold" lasts for the current route only. A later route may call the key again.
    # Persistent credential blocks are reserved for terminal billing and auth.
    blocks = "" if failover else "hold"
    return _decision(
        failure_class,
        retryable=True,
        failover_allowed=failover,
        retry_kind=kind,
        problem_slug=slug,
        http_status=http_status,
        blocks=blocks,
        retry_after_seconds=seconds,
        retry_after_parse=parsed,
        lab_rule=lab_rule,
    )
