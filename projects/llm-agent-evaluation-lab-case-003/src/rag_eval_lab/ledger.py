"""Append-only confabulation ledger keyed by provider, prompt hash, and set id."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import LabError


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class LedgerRow:
    provider_id: str
    prompt_hash: str
    regression_set_id: str
    numerator: int
    denominator: int
    citation_defect_numerator: int
    citation_defect_denominator: int

    @property
    def rate(self) -> float | None:
        if self.denominator == 0:
            return None
        return self.numerator / self.denominator


class ConfabulationLedger:
    def __init__(self) -> None:
        self.rows: list[LedgerRow] = []

    def record(
        self,
        provider_id: str,
        prompt_hash_value: str,
        regression_set_id: str,
        numerator: int,
        denominator: int,
        citation_defect_numerator: int,
        citation_defect_denominator: int,
    ) -> LedgerRow:
        for row in self.rows:
            if (
                row.provider_id == provider_id
                and row.prompt_hash == prompt_hash_value
                and row.regression_set_id == regression_set_id
            ):
                if (
                    row.numerator != numerator
                    or row.denominator != denominator
                    or row.citation_defect_numerator != citation_defect_numerator
                    or row.citation_defect_denominator != citation_defect_denominator
                ):
                    raise LabError("same provider and prompt hash reproduced a different rate")
                return row
        row = LedgerRow(
            provider_id=provider_id,
            prompt_hash=prompt_hash_value,
            regression_set_id=regression_set_id,
            numerator=numerator,
            denominator=denominator,
            citation_defect_numerator=citation_defect_numerator,
            citation_defect_denominator=citation_defect_denominator,
        )
        self.rows.append(row)
        return row

    def to_json(self) -> dict[str, Any]:
        return {"rows": [asdict(row) | {"rate": row.rate} for row in self.rows]}

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "ConfabulationLedger":
        payload = json.loads(path.read_text(encoding="utf-8"))
        ledger = cls()
        for row in payload.get("rows") or []:
            ledger.rows.append(
                LedgerRow(
                    provider_id=row["provider_id"],
                    prompt_hash=row["prompt_hash"],
                    regression_set_id=row["regression_set_id"],
                    numerator=row["numerator"],
                    denominator=row["denominator"],
                    citation_defect_numerator=row["citation_defect_numerator"],
                    citation_defect_denominator=row["citation_defect_denominator"],
                )
            )
        return ledger
