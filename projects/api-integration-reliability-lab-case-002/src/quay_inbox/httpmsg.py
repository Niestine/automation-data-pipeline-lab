"""In-process HTTP shapes. No sockets are opened."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    authority: str
    headers: dict[str, str]
    body: bytes
    client_id: str = "anonymous"
    peer: str = ""
    sni: str = ""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


def plain(status: int, error: str, **headers: str) -> HttpResponse:
    payload = json.dumps({"error": error}, separators=(",", ":")).encode("utf-8")
    base = {"content-type": "application/json"}
    base.update(headers)
    return HttpResponse(status, payload, base)


def problem(status: int, title: str, detail: str) -> HttpResponse:
    payload = {
        "type": "https://developer.example.com/idempotency",
        "title": title,
        "detail": detail,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return HttpResponse(
        status,
        raw,
        {"content-type": "application/problem+json"},
    )


def normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}
