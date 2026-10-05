"""Error handlers. The broken ones are the maintenance fixtures the scanner flags."""

from __future__ import annotations

import inspect

from .errors import (
    BudgetExhausted,
    OperationAborted,
    OverloadedSignal,
    PermanentGateError,
    TransientGateError,
    WrappedHandlerError,
)
from .log import LOGGER
from .policy import RetryPolicy


def empty_handler(error: BaseException) -> str:
    # FIXME: incident note left this handler empty and the desk still reported the bay posted.
    return "fake-success"


def swallow_handler(error: BaseException) -> str:
    # TODO: log-only path from the March maintenance note; the notice still looks posted.
    LOGGER.info("gate error ignored")
    return "fake-success"


def rewrite_handler(error: BaseException) -> str:
    raise WrappedHandlerError(type(error).__name__)


def broad_abort(error: BaseException) -> str:
    try:
        raise error
    except Exception as exc:
        raise OperationAborted(type(exc).__name__) from exc


def repair_handler(error: BaseException, policy: RetryPolicy, defects: object) -> str:
    if isinstance(error, BudgetExhausted):
        raise error
    if isinstance(error, OverloadedSignal):
        return "overloaded"
    retryable = policy.allows(error)
    if getattr(defects, "retry_permanent", False) and type(error) is PermanentGateError:
        retryable = True
    if getattr(defects, "skip_transient", False) and type(error) is TransientGateError:
        retryable = False
    if retryable:
        return "retry"
    raise error


def dispatch_error(handler: str, error: BaseException, policy: RetryPolicy, defects: object) -> str:
    if handler == "empty":
        return empty_handler(error)
    if handler == "swallow":
        return swallow_handler(error)
    if handler == "rewrite":
        return rewrite_handler(error)
    if handler == "broad":
        return broad_abort(error)
    if handler == "repair":
        return repair_handler(error, policy, defects)
    raise ValueError(f"unknown handler {handler!r}")


def handler_markers(fn: object) -> list[str]:
    text = inspect.getsource(fn)
    return [marker for marker in ("FIXME", "TODO") if marker in text]
