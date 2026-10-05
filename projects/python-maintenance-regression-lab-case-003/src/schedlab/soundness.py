"""Refuse subjects that sneak a clock or an unseeded draw past the scheduler.

A controlled scheduler that still sleeps or reads the clock is unsound: the
result is no longer a function of the recorded thread ids.
"""

from __future__ import annotations

import datetime
import os
import random
import time
import types
from typing import Any, Callable

from schedlab.errors import SoundnessError

_FORBIDDEN = {
    "time": time,
    "random": random,
    "datetime": datetime,
}


def assert_sound(subject: object) -> None:
    """Raise ``SoundnessError`` before any artifact is written.

    The predicate and any ``audit_fns`` are walked. So are the plain functions
    they reach through globals or closures, and the code of nested lambdas.
    """

    functions: list[Callable[..., Any]] = []
    predicate = getattr(subject, "predicate", None)
    if predicate is not None:
        functions.append(predicate)
    functions.extend(getattr(subject, "audit_fns", ()))
    seen: set[int] = set()
    for function in functions:
        _walk(function, seen)


def _walk(function: Callable[..., Any], seen: set[int]) -> None:
    if id(function) in seen:
        return
    code = getattr(function, "__code__", None)
    if code is None:
        if _forbidden_value(function):
            raise SoundnessError("subject callable is a clock or RNG")
        return
    seen.add(id(function))
    names = _code_names(code)
    if "urandom" in names:
        raise SoundnessError("subject references os.urandom")
    leaked = names & set(_FORBIDDEN)
    if leaked:
        raise SoundnessError(f"subject references {sorted(leaked)}")
    globals_ = getattr(function, "__globals__", {})
    for name in sorted(names):
        value = globals_.get(name)
        if _forbidden_value(value):
            raise SoundnessError(f"subject global {name} is not controlled")
        if isinstance(value, types.FunctionType):
            _walk(value, seen)
    closure = getattr(function, "__closure__", None)
    if not closure:
        return
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if _forbidden_value(value):
            raise SoundnessError("subject closure captures a clock or RNG")
        if isinstance(value, types.FunctionType):
            _walk(value, seen)


def _code_names(code: types.CodeType) -> set[str]:
    names = set(code.co_names) | set(code.co_freevars) | set(code.co_cellvars)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _code_names(const)
    return names


def _forbidden_value(value: object) -> bool:
    if value is None:
        return False
    if value is os.urandom:
        return True
    if isinstance(value, types.ModuleType):
        return value.__name__.split(".")[0] in _FORBIDDEN
    # ``from time import sleep`` or ``from datetime import datetime`` binds a
    # name whose owner module is still a clock or RNG.
    owner = getattr(value, "__module__", None)
    if isinstance(owner, str) and owner.split(".")[0] in _FORBIDDEN:
        return True
    # ``from random import random`` binds a method of the hidden global Random.
    bound = getattr(value, "__self__", None)
    if bound is not None and not isinstance(bound, types.ModuleType):
        bound_owner = getattr(type(bound), "__module__", "")
        if isinstance(bound, type):
            bound_owner = bound.__module__
        if bound_owner.split(".")[0] in _FORBIDDEN:
            return True
    return False
