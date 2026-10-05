"""Non-idempotent bay charge. The repaired key is the operation id alone."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import JobRejected

_OPERATION = re.compile(r"^BAY-[0-9]{4}$")
_BAY = re.compile(r"^[A-Z]-[0-9]{2}$")
# ASCII digits only. str.isdigit would also accept other scripts' digits.
_CHARGE = re.compile(r"^([0-9]+)\.([0-9]{2})$")


def charge_cents(charge: str) -> int:
    match = _CHARGE.fullmatch(charge)
    if match is None:
        raise JobRejected(f"charge {charge!r} needs two decimal places")
    return int(match.group(1)) * 100 + int(match.group(2))


@dataclass
class Entry:
    operation_id: str
    token: str
    body: str
    charge_cents: int


class Ledger:
    def __init__(self) -> None:
        self.entries: list[Entry] = []

    def commit(self, operation_id: str, token: str, body: str, charge: str) -> bool:
        if any(entry.operation_id == operation_id for entry in self.entries):
            return False
        self.entries.append(
            Entry(operation_id, token, body, charge_cents(charge))
        )
        return True

    def commit_token(self, operation_id: str, token: str, body: str, charge: str) -> bool:
        """Broken key. A second attempt token charges the bay again."""

        if any(
            entry.operation_id == operation_id and entry.token == token
            for entry in self.entries
        ):
            return False
        self.entries.append(
            Entry(operation_id, token, body, charge_cents(charge))
        )
        return True

    @property
    def charge_cents_total(self) -> int:
        return sum(entry.charge_cents for entry in self.entries)


def validate_job(operation_id: str, bay: str, body: str, charge: str) -> None:
    if not _OPERATION.fullmatch(operation_id):
        raise JobRejected(f"operation id {operation_id!r} is not BAY-nnnn")
    if not _BAY.fullmatch(bay):
        raise JobRejected(f"bay {bay!r} is not L-dd")
    if not body or body.strip() != body:
        raise JobRejected("body must be non-empty and trimmed")
    charge_cents(charge)
