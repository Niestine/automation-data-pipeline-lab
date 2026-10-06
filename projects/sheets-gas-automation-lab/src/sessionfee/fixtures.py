"""Load synthetic fixtures. Paths stay inside this project."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from sessionfee.canonical import SessionInput
from sessionfee.schema import SessionRow, row_from_mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = PROJECT_ROOT / "examples"


def load_json(name: str) -> dict[str, Any]:
    path = EXAMPLES / name
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return data


def sessions_from_document(document: dict[str, Any]) -> list[SessionInput]:
    sessions: list[SessionInput] = []
    for raw in document["sessions"]:
        sessions.append(
            SessionInput(
                operation_id=str(raw["operation_id"]),
                session_date=str(raw["session_date"]),
                desk_code=str(raw["desk_code"]),
                hours=Decimal(str(raw["hours"])),
                rate_jpy_per_hour=Decimal(str(raw["rate_jpy_per_hour"])),
            )
        )
    return sessions


def rows_from_document(document: dict[str, Any]) -> list[SessionRow]:
    return [row_from_mapping(raw) for raw in document["rows"]]


def decimal_field(document: dict[str, Any], name: str) -> Decimal:
    return Decimal(str(document[name]))
