"""RFC 6902 apply on a copy, plus the contract mask in front of it."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .pointer import (
    PointerError,
    is_index,
    parent_and_token,
    parse_pointer,
    pointer_get,
    proper_prefix,
)
from .util import json_equal

DEFAULT_IMMUTABLE = (
    "/tool",
    "/case_id",
    "/user_id",
    "/budget",
    "/attempts",
    "/op_budget",
    "/ledger",
)
ALLOWED_OPS = ("add", "remove", "replace", "move", "copy", "test")
MASK_OPS = ("add", "remove", "replace", "test")


@dataclass
class ApplyResult:
    ok: bool
    document: Any
    reason: str | None = None
    op_index: int | None = None


@dataclass
class MaskResult:
    ok: bool
    reason: str
    op_index: int | None = None
    ignored_members: list[dict[str, Any]] = field(default_factory=list)
    non_test_ops: int = 0


class PatchApplyError(Exception):
    def __init__(self, reason: str, op_index: int):
        super().__init__(reason)
        self.reason = reason
        self.op_index = op_index


def _fail(reason: str, op_index: int | None, ignored: list[dict[str, Any]], non_test: int) -> MaskResult:
    return MaskResult(False, reason, op_index, ignored, non_test)


def _hits_immutable(path: str, immutable: list[str]) -> bool:
    for item in immutable:
        if path == item or path.startswith(item + "/") or item.startswith(path + "/"):
            return True
    return False


def mask_patch(
    ops: Any,
    *,
    document: Any,
    repair: dict[str, Any] | None,
    immutable_paths: list[str] | None = None,
    allow_move_copy: bool = False,
    op_budget: int = 2,
) -> MaskResult:
    ignored: list[dict[str, Any]] = []
    if not isinstance(ops, list):
        return _fail("not_array", 0, ignored, 0)
    immutable = list(DEFAULT_IMMUTABLE if immutable_paths is None else immutable_paths)
    non_test = 0
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            return _fail("bad_op", index, ignored, non_test)
        extra = sorted(key for key in op if key not in {"op", "path", "from", "value"})
        if extra:
            ignored.append({"index": index, "members": extra})
        name = op.get("op")
        path = op.get("path")
        if not isinstance(name, str) or name not in ALLOWED_OPS or not isinstance(path, str):
            return _fail("bad_op", index, ignored, non_test)
        if name in {"move", "copy"} and not allow_move_copy:
            return _fail("move_copy_disabled", index, ignored, non_test)
        try:
            parse_pointer(path)
        except PointerError:
            return _fail("bad_pointer", index, ignored, non_test)
        if name in {"move", "copy"}:
            source = op.get("from")
            if not isinstance(source, str):
                return _fail("missing_from", index, ignored, non_test)
            try:
                parse_pointer(source)
            except PointerError:
                return _fail("bad_pointer", index, ignored, non_test)
            if name == "move" and proper_prefix(source, path):
                return _fail("move_prefix", index, ignored, non_test)
        if name != "test" and _hits_immutable(path, immutable):
            return _fail("immutable_path", index, ignored, non_test)
        if name in {"move", "copy"} and isinstance(op.get("from"), str) and _hits_immutable(op["from"], immutable):
            return _fail("immutable_path", index, ignored, non_test)
        if name in {"replace", "remove"}:
            previous = ops[index - 1] if index else None
            if (
                not isinstance(previous, dict)
                or previous.get("op") != "test"
                or previous.get("path") != path
            ):
                return _fail("missing_test", index, ignored, non_test)
            if repair and path == repair.get("location"):
                if not json_equal(previous.get("value"), repair.get("observed")):
                    return _fail("test_observed_mismatch", index, ignored, non_test)
            else:
                try:
                    actual = pointer_get(document, path)
                except PointerError:
                    return _fail("path_missing", index, ignored, non_test)
                if not json_equal(previous.get("value"), actual):
                    return _fail("test_document_mismatch", index, ignored, non_test)
        if name != "test":
            non_test += 1
    if non_test > op_budget:
        return _fail("over_budget", None, ignored, non_test)
    return MaskResult(True, "ok", None, ignored, non_test)


def _add(document: Any, pointer: str, value: Any, op_index: int) -> Any:
    if pointer == "":
        return deepcopy(value)
    try:
        parent, token = parent_and_token(document, pointer)
    except PointerError as exc:
        raise PatchApplyError("parent_missing", op_index) from exc
    if isinstance(parent, dict):
        parent[token] = deepcopy(value)
        return document
    if isinstance(parent, list):
        if token == "-":
            parent.append(deepcopy(value))
            return document
        if not is_index(token):
            raise PatchApplyError("bad_index", op_index)
        index = int(token)
        if index > len(parent):
            raise PatchApplyError("index_oob", op_index)
        parent.insert(index, deepcopy(value))
        return document
    raise PatchApplyError("parent_type", op_index)


def _remove(document: Any, pointer: str, op_index: int) -> Any:
    if pointer == "":
        raise PatchApplyError("not_found", op_index)
    try:
        parent, token = parent_and_token(document, pointer)
    except PointerError as exc:
        raise PatchApplyError("not_found", op_index) from exc
    if isinstance(parent, dict):
        if token not in parent:
            raise PatchApplyError("not_found", op_index)
        del parent[token]
        return document
    if isinstance(parent, list):
        if not is_index(token):
            raise PatchApplyError("bad_index", op_index)
        index = int(token)
        if index >= len(parent):
            raise PatchApplyError("not_found", op_index)
        del parent[index]
        return document
    raise PatchApplyError("parent_type", op_index)


def _replace(document: Any, pointer: str, value: Any, op_index: int) -> Any:
    if pointer == "":
        return deepcopy(value)
    try:
        pointer_get(document, pointer)
    except PointerError as exc:
        raise PatchApplyError("not_found", op_index) from exc
    try:
        parent, token = parent_and_token(document, pointer)
    except PointerError as exc:
        raise PatchApplyError("not_found", op_index) from exc
    if isinstance(parent, dict):
        parent[token] = deepcopy(value)
        return document
    if isinstance(parent, list):
        if not is_index(token):
            raise PatchApplyError("bad_index", op_index)
        index = int(token)
        if index >= len(parent):
            raise PatchApplyError("not_found", op_index)
        parent[index] = deepcopy(value)
        return document
    raise PatchApplyError("parent_type", op_index)


def _apply_one(document: Any, op: dict[str, Any], op_index: int) -> Any:
    name = op.get("op")
    path = op.get("path")
    if not isinstance(name, str) or not isinstance(path, str):
        raise PatchApplyError("bad_op", op_index)
    if name == "add":
        if "value" not in op:
            raise PatchApplyError("missing_value", op_index)
        return _add(document, path, op["value"], op_index)
    if name == "remove":
        return _remove(document, path, op_index)
    if name == "replace":
        if "value" not in op:
            raise PatchApplyError("missing_value", op_index)
        return _replace(document, path, op["value"], op_index)
    if name == "test":
        if "value" not in op:
            raise PatchApplyError("missing_value", op_index)
        try:
            current = pointer_get(document, path)
        except PointerError as exc:
            raise PatchApplyError("not_found", op_index) from exc
        if not json_equal(current, op["value"]):
            raise PatchApplyError("test_mismatch", op_index)
        return document
    if name in {"move", "copy"}:
        source = op.get("from")
        if not isinstance(source, str):
            raise PatchApplyError("missing_from", op_index)
        if name == "move" and proper_prefix(source, path):
            raise PatchApplyError("move_prefix", op_index)
        try:
            value = pointer_get(document, source)
        except PointerError as exc:
            raise PatchApplyError("not_found", op_index) from exc
        if name == "move":
            document = _remove(document, source, op_index)
        return _add(document, path, value, op_index)
    raise PatchApplyError("bad_op", op_index)


def apply_patch(document: Any, ops: list[dict[str, Any]]) -> ApplyResult:
    """Apply ops to a deep copy. On the first error the original object is returned."""
    if not isinstance(ops, list):
        return ApplyResult(False, document, "not_array", 0)
    working = deepcopy(document)
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            return ApplyResult(False, document, "bad_op", index)
        try:
            working = _apply_one(working, op, index)
        except PatchApplyError as exc:
            return ApplyResult(False, document, exc.reason, exc.op_index)
    return ApplyResult(True, working, None, None)
