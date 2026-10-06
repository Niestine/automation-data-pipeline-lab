"""In-process HTTP messages. No socket is opened."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from lotcycle.errors import ConnectionClosed, ResponseDropped


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""

    def __post_init__(self) -> None:
        self.method = self.method.upper()
        self.headers = {key.lower(): value for key, value in self.headers.items()}


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes = b""

    def __post_init__(self) -> None:
        self.headers = {key.lower(): value for key, value in self.headers.items()}

    def json(self) -> dict:
        parsed = json.loads(self.body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("response json")
        return parsed


def parse_form(body: bytes) -> dict[str, str] | None:
    """Return a single-value form, or None when a parameter is repeated."""
    from urllib.parse import parse_qs

    text = body.decode("utf-8")
    parsed = parse_qs(text, keep_blank_values=True, strict_parsing=False)
    form: dict[str, str] = {}
    for key, values in parsed.items():
        if len(values) != 1:
            return None
        form[key] = values[0]
    return form


def parse_link(header: str | None) -> dict[str, str]:
    """Map rel to URL. Link targets in this lab do not contain commas."""
    found: dict[str, str] = {}
    if not header:
        return found
    for part in header.split(","):
        part = part.strip()
        if not part.startswith("<"):
            continue
        url, _, rest = part[1:].partition(">")
        rel = None
        for item in rest.split(";"):
            item = item.strip()
            if item.startswith("rel="):
                rel = item[4:].strip().strip('"')
        if rel and url:
            found[rel] = url
    return found


@dataclass
class Transport:
    """Routes /token, /{audience}/..., and /hooks/... to in-process handlers."""

    handlers: dict[str, object]
    fail_before_send: int = 0
    drop_after_send: int = 0
    attempts: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)

    def exchange(self, request: Request) -> Response:
        self.attempts.append(request.path)
        if self.fail_before_send > 0:
            self.fail_before_send -= 1
            raise ConnectionClosed(request.path)
        handler = self._handler_for(request.path)
        self.sent.append(request.path)
        response = handler(request)
        if self.drop_after_send > 0:
            self.drop_after_send -= 1
            raise ResponseDropped(request.path)
        return response

    def _handler_for(self, path: str):
        if path == "/token":
            return self.handlers["token"]
        if path.startswith("/hooks/"):
            return self.handlers["hooks"]
        audience = path.strip("/").split("/", 1)[0]
        try:
            return self.handlers[audience]
        except KeyError as exc:
            raise KeyError(path) from exc
