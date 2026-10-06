"""Structured events for commit outcomes. No network sink."""

from __future__ import annotations

import json
import logging
from typing import Any


LOGGER = logging.getLogger("sessionfee")


class RunLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def add(self, **event: Any) -> None:
        record = dict(event)
        self.events.append(record)
        LOGGER.info("%s", json.dumps(record, default=str, sort_keys=True))
