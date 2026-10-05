"""JSON loaders for the tide-desk fixtures."""

from __future__ import annotations

import json
from pathlib import Path


def read_json(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)
