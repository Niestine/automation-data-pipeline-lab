"""RFC 6901 JSON Pointers."""

from __future__ import annotations

from typing import Any


class PointerError(ValueError):
    def __init__(self, reason: str, pointer: str = ""):
        super().__init__(f"{reason}: {pointer}" if pointer else reason)
        self.reason = reason
        self.pointer = pointer


def encode_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def decode_token(token: str) -> str:
    # ~1 must be replaced before ~0.
    return token.replace("~1", "/").replace("~0", "~")


def parse_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise PointerError("bad_pointer", str(pointer))
    return [decode_token(part) for part in pointer[1:].split("/")]


def join_pointer(tokens: list[str]) -> str:
    if not tokens:
        return ""
    return "/" + "/".join(encode_token(token) for token in tokens)


def is_index(token: str) -> bool:
    return token.isdigit() and (token == "0" or not token.startswith("0"))


def proper_prefix(left: str, right: str) -> bool:
    """True when left is a proper JSON Pointer prefix of right."""
    if left == right:
        return False
    if left == "":
        return right != ""
    return right.startswith(left + "/")


def _step(current: Any, token: str) -> Any:
    if isinstance(current, dict):
        if token not in current:
            raise PointerError("missing", token)
        return current[token]
    if isinstance(current, list):
        if token == "-" or not is_index(token):
            raise PointerError("bad_index", token)
        index = int(token)
        if index >= len(current):
            raise PointerError("missing", token)
        return current[index]
    raise PointerError("parent_type", token)


def pointer_get(document: Any, pointer: str) -> Any:
    current = document
    for token in parse_pointer(pointer):
        current = _step(current, token)
    return current


def parent_and_token(document: Any, pointer: str) -> tuple[Any, str]:
    tokens = parse_pointer(pointer)
    if not tokens:
        raise PointerError("empty", pointer)
    current = document
    for token in tokens[:-1]:
        try:
            current = _step(current, token)
        except PointerError as exc:
            raise PointerError("parent_missing", pointer) from exc
    return current, tokens[-1]
