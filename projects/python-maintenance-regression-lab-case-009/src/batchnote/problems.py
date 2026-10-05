"""One problem value, rendered as a human line or a JSON object."""

from __future__ import annotations

import json
from typing import Mapping, Optional, TextIO

from batchnote import status

CATALOG = {
    "usage": (
        "urn:batchnote:problem:usage",
        "Command line usage error",
        status.USAGE,
    ),
    "data": (
        "urn:batchnote:problem:data",
        "Input data error",
        status.FAILURE,
    ),
    "no-input": (
        "urn:batchnote:problem:no-input",
        "Input unavailable",
        status.FAILURE,
    ),
    "io": (
        "urn:batchnote:problem:io",
        "Input/output error",
        status.FAILURE,
    ),
    "config": (
        "urn:batchnote:problem:config",
        "Configuration error",
        status.FAILURE,
    ),
    "os": (
        "urn:batchnote:problem:os",
        "Operating system error",
        status.FAILURE,
    ),
    "software": (
        "urn:batchnote:problem:software",
        "Internal software error",
        status.FAILURE,
    ),
}

_TYPE_PREFIX = "urn:batchnote:problem:"


def normalize_detail(detail: str) -> str:
    text = " ".join(detail.split())
    if text.endswith("."):
        text = text[:-1]
    if text[:1].isalpha():
        text = text[0].lower() + text[1:]
    if not text:
        text = "unspecified failure"
    return text


class Problem:
    """Occurrence of one catalogued failure. `detail` is prose, not an API."""

    def __init__(
        self,
        kind: str,
        detail: str,
        locator: Optional[Mapping[str, object]] = None,
        suggestion: Optional[str] = None,
    ) -> None:
        try:
            type_urn, title, code = CATALOG[kind]
        except KeyError as exc:
            raise ValueError(f"unknown problem kind: {kind}") from exc
        self.kind = kind
        self.type = type_urn
        self.title = title
        self.status = code
        self.detail = normalize_detail(detail)
        self.locator = dict(locator) if locator else None
        self.suggestion = suggestion
        if self.detail.casefold() == self.title.casefold():
            raise ValueError("detail must not reuse title")


def render_human(problem: Problem, prog: str) -> str:
    locator = problem.locator or {}
    path = locator.get("path")
    line = locator.get("line")
    column = locator.get("column")
    if path is not None and line is not None and column is not None:
        head = f"{prog}: {path}:{line}:{column}: "
    elif path is not None:
        head = f"{prog}: {path}: "
    else:
        head = f"{prog}: "
    return head + problem.detail + "\n"


def render_json(problem: Problem) -> str:
    payload = {
        "type": problem.type,
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
    }
    if problem.locator:
        payload["locator"] = {
            key: value
            for key, value in problem.locator.items()
            if value is not None
        }
    if problem.suggestion:
        payload["suggestion"] = problem.suggestion
    return json.dumps(payload, ensure_ascii=True) + "\n"


def _write(stream: TextIO, text: str) -> bool:
    try:
        stream.write(text)
    except OSError:
        return False
    return True


def emit(problem: Problem, stderr: TextIO, *, prog: str, report: str) -> bool:
    """Write one problem. Human mode may add one closest-match line."""
    if report == "json":
        return _write(stderr, render_json(problem))
    if not _write(stderr, render_human(problem, prog)):
        return False
    if not problem.suggestion:
        return True
    hint = Problem("usage", f"closest match: {problem.suggestion}")
    return _write(stderr, render_human(hint, prog))


def consume(document: Mapping[str, object]) -> str:
    """Return the problem type. Unknown members are ignored. `detail` is not read."""
    if not isinstance(document, Mapping):
        raise ValueError("problem document")
    type_urn = document.get("type")
    if not isinstance(type_urn, str) or not type_urn.startswith(_TYPE_PREFIX):
        raise ValueError("type")
    if type_urn == _TYPE_PREFIX:
        raise ValueError("type")
    return type_urn
