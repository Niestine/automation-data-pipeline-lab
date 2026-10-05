"""One structured record per transmission. Tests read attributes, not the sentence."""

from __future__ import annotations

import json
import logging

LOGGER = logging.getLogger("bay_notice.attempt")
# Library convention: no level, no propagation change. The caller decides where records go.
LOGGER.addHandler(logging.NullHandler())

FIELDS = (
    "operation_id",
    "attempt",
    "token",
    "error_type",
    "decision",
    "delay_s",
    "rto_s",
)


def emit_attempt(
    template: str,
    operation_id: str,
    attempt: int,
    token: str,
    error_type: str,
    decision: str,
    delay_s: float,
    rto_s: float,
) -> None:
    if decision not in {"retry", "stop", "budget_refuse", "not_retryable"}:
        raise ValueError(f"unknown decision {decision!r}")
    fields = {
        "operation_id": operation_id,
        "attempt": attempt,
        "token": token,
        "error_type": error_type,
        "decision": decision,
        "delay_s": delay_s,
        "rto_s": rto_s,
    }
    LOGGER.info(template.format(**fields), extra=fields)


class JsonFieldFormatter(logging.Formatter):
    """One JSON object per record, built from the attributes rather than the sentence."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {name: getattr(record, name) for name in FIELDS if hasattr(record, name)}
        payload["message"] = record.getMessage()
        return json.dumps(payload, sort_keys=True)
