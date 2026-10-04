"""Resolve $step.path bindings against prior tool payloads."""

from __future__ import annotations

from typing import Any
import re


class BindError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_EXPR = re.compile(r"^\$([A-Za-z][A-Za-z0-9_]*)(.*)$")
_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def step_id_from_expr(expr: str) -> str:
    if not isinstance(expr, str):
        raise BindError("bind_syntax", "bind expression must be a string")
    match = _EXPR.match(expr.strip())
    if match is None:
        raise BindError("bind_syntax", f"invalid bind expression {expr!r}")
    return match.group(1)


def resolve_path(payload: Any, rest: str) -> Any:
    current = payload
    pos = 0
    for token in _TOKEN.finditer(rest):
        if token.start() != pos:
            raise BindError("bind_syntax", f"invalid bind path {rest!r}")
        pos = token.end()
        key = token.group(1)
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                raise BindError("bind_path", f"missing object key {key!r}")
            current = current[key]
            continue
        index = int(token.group(2))
        if not isinstance(current, list) or index >= len(current) or index < 0:
            raise BindError("bind_path", f"missing list index {index}")
        current = current[index]
    if pos != len(rest):
        raise BindError("bind_syntax", f"invalid bind path {rest!r}")
    return current


def resolve_expr(expr: str, completed: dict[str, Any]) -> Any:
    if not isinstance(expr, str):
        raise BindError("bind_syntax", "bind expression must be a string")
    match = _EXPR.match(expr.strip())
    if match is None:
        raise BindError("bind_syntax", f"invalid bind expression {expr!r}")
    step_id = match.group(1)
    rest = match.group(2)
    if step_id not in completed:
        raise BindError("bind_unresolved", f"step {step_id} has no payload yet")
    return resolve_path(completed[step_id], rest)


def resolve_bind_map(bind: dict[str, str], completed: dict[str, Any]) -> dict[str, Any]:
    return {key: resolve_expr(expr, completed) for key, expr in bind.items()}
