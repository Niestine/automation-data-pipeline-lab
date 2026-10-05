"""Version tuples from Serbout and Pautasso's prose, not their damaged regular expression.

The paper's printed expression is whitespace-damaged and has an unbalanced
parenthesis. Parsing follows the prose and the two examples: an optional
leading ``v``, one to three numeric components of one to three digits, and an
optional label from a closed set. Missing minor and patch become 0. This is
not Semantic Versioning 2.0.0 precedence: ``1.2.3-alpha`` to ``1.2.3`` is a
label change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from contract_lab.errors import VersionParseError

LABELS = frozenset(
    {"alpha", "beta", "dev", "snapshot", "rc", "preview", "test", "private"}
)

_VERSION = re.compile(
    r"^[vV]?"
    r"(?P<major>[0-9]{1,3})"
    r"(?:\.(?P<minor>[0-9]{1,3}))?"
    r"(?:\.(?P<patch>[0-9]{1,3}))?"
    r"(?:-(?P<label>[A-Za-z0-9]+))?$"
)


@dataclass(frozen=True)
class VersionTuple:
    major: int
    minor: int
    patch: int
    label: str = ""

    def as_tuple(self) -> tuple[int, int, int, str]:
        return (self.major, self.minor, self.patch, self.label)


def parse_version(value: str) -> VersionTuple:
    if not isinstance(value, str):
        raise VersionParseError(str(value))
    match = _VERSION.fullmatch(value)
    if match is None:
        raise VersionParseError(value)
    label = match.group("label") or ""
    if label and label not in LABELS:
        raise VersionParseError(value)
    minor = match.group("minor")
    patch = match.group("patch")
    return VersionTuple(
        major=int(match.group("major")),
        minor=int(minor) if minor is not None else 0,
        patch=int(patch) if patch is not None else 0,
        label=label,
    )


def classify_change(left: VersionTuple, right: VersionTuple) -> str:
    """First differing component wins. Numbers compare as integers."""

    if left.major != right.major:
        return "major_upgrade" if left.major < right.major else "major_downgrade"
    if left.minor != right.minor:
        return "minor_upgrade" if left.minor < right.minor else "minor_downgrade"
    if left.patch != right.patch:
        return "patch_upgrade" if left.patch < right.patch else "patch_downgrade"
    if left.label != right.label:
        return "label_change"
    return "no_change"
