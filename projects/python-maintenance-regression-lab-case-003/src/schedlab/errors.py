"""Typed failures for the schedule lab."""

from __future__ import annotations


class SchedlabError(Exception):
    """Base class for errors raised by schedlab."""


class MachineError(SchedlabError):
    """The step machine was used incorrectly."""


class SoundnessError(SchedlabError):
    """A subject closed over a clock, a sleep, or an unseeded RNG."""


class ArtifactError(SchedlabError):
    """A schedule artifact is missing fields or has the wrong shape."""
