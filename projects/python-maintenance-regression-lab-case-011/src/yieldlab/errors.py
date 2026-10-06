"""Classified failures from the yield-point simulator.

``run`` either returns a trace or raises one of these. The journal on the
exception is the schedule prefix that reached the failure.
"""

from __future__ import annotations


class LabError(Exception):
    def __init__(self, message: str, journal: dict | None = None) -> None:
        super().__init__(message)
        self.journal = journal


class Deadlock(LabError):
    pass


class Race(LabError):
    pass


class AtomicityViolation(LabError):
    pass


class Invariant(LabError):
    pass


class UseAfterFree(LabError):
    pass


class NeverRetrieved(LabError):
    pass


class WrongThread(LabError):
    pass


class GeneratorReentered(LabError):
    pass


class ReplayDivergence(LabError):
    pass


class BoundExceeded(LabError):
    pass
