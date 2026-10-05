"""RFC 9457 problem documents for router failures.

`type` is the classification key. `detail` is human text and is not parsed.
Missing `type` is read as `about:blank`. Unknown extension members are ignored
by `core_view`.
"""

from __future__ import annotations

PROBLEM_CONTENT_TYPE = "application/problem+json"
TYPE_PREFIX = "https://portfolio.example/problems/"

# slug -> (HTTP status, stable title)
CATALOG: dict[str, tuple[int, str]] = {
    "budget-exhausted": (402, "Budget exhausted"),
    "caller-quota": (429, "Caller quota exceeded"),
    "input-too-large": (413, "Input too large"),
    "terminal-billing": (402, "Terminal billing failure"),
    "rate-limited": (429, "Rate limited"),
    "overloaded": (503, "Provider overloaded"),
    "timeout": (504, "Provider timeout"),
    "request-defect": (400, "Request defect"),
    "unavailable": (503, "Provider unavailable"),
    "conflict": (409, "Conflict"),
    "unauthorized": (401, "Credential rejected"),
    "network": (511, "Network authentication required"),
    "contract-invalid": (422, "Slip contract rejected"),
}


def problem_type(slug: str) -> str:
    return TYPE_PREFIX + slug


def make_problem(
    slug: str,
    *,
    detail: str,
    instance: str,
    provider: str | None = None,
    model: str | None = None,
    failure_class: str | None = None,
    retryable: bool | None = None,
    failover_allowed: bool | None = None,
    retry_after_ms: int | None = None,
    budget_remaining: float | None = None,
    request_id: str | None = None,
    status: int | None = None,
) -> dict:
    """One problem document. Status defaults to the catalog status for `slug`."""
    catalog_status, title = CATALOG[slug]
    body: dict = {
        "type": problem_type(slug),
        "status": catalog_status if status is None else status,
        "title": title,
        "detail": detail,
        "instance": instance,
    }
    extensions = {
        "provider": provider,
        "model": model,
        "failure_class": failure_class,
        "retryable": retryable,
        "failover_allowed": failover_allowed,
        "retry_after_ms": retry_after_ms,
        "budget_remaining": budget_remaining,
        "request_id": request_id,
    }
    for key, value in extensions.items():
        if value is not None:
            body[key] = value
    return body


def core_view(document: dict) -> dict:
    """What a client keeps after dropping unknown extension members."""
    raw = document.get("type")
    return {
        "type": raw if raw else "about:blank",
        "status": document.get("status"),
    }
