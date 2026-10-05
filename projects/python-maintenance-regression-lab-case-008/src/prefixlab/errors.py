"""Failures that carry the oracle payload tests assert on."""

from __future__ import annotations

import json


class OracleFault(Exception):
    """A crash or anomaly oracle failed.

    ``payload`` always has ``outcome`` and ``transactions``. It also has
    ``cut`` or ``stable_lsn`` when the failure is a prefix cut.
    """

    def __init__(self, payload: dict):
        self.payload = dict(payload)
        super().__init__(json.dumps(self.payload, sort_keys=True, default=str))
