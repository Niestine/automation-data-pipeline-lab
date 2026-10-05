"""Typed gate outcomes. The retry predicate matches these classes exactly."""

from __future__ import annotations


class BayError(Exception):
    """Base for errors the desk itself raises."""


class JobRejected(BayError):
    """The notice failed local validation and was not sent."""


class TransientGateError(BayError):
    """The gate signaled a failure the repaired desk may retry."""


class PermanentGateError(BayError):
    """The gate rejected the notice. Retrying will not change the answer."""


class OverloadedSignal(BayError):
    """The gate asked callers to stop adding retry load."""


class BudgetExhausted(BayError):
    """The shared retry budget refused this attempt. Callers must not retry it."""


class OperationAborted(BayError):
    """A broad handler aborted a non-fatal gate error."""


class WrappedHandlerError(BayError):
    """A handler replaced the gate error with a different type."""


class OracleFailure(BayError):
    """A repair oracle rejected a build."""


def make_error(name: str) -> BayError:
    if name == "transient":
        return TransientGateError("gate timed out")
    if name == "permanent":
        return PermanentGateError("bay unknown")
    if name == "overloaded":
        return OverloadedSignal("gate overloaded")
    raise ValueError(f"unknown gate error {name!r}")
