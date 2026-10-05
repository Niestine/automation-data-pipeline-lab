"""Logger name shared by the client and the command line.

The library adds only a ``NullHandler``. Records propagate as usual, so an
application that configures the root logger sees them; nothing is printed
until some handler is attached.
"""

from __future__ import annotations

import json
import logging

LOGGER_NAME = "contract_lab"

logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_event(event) -> None:
    """One record per event: kind and operation first, then the non-empty fields as JSON.

    The full event dict is also attached as ``record.event`` for handlers that
    want structured data instead of text.
    """

    level = logging.INFO
    if event.kind == "clock_order_error":
        level = logging.ERROR
    elif event.kind in {"deprecation_warning", "schema_rejected"}:
        level = logging.WARNING
    payload = event.as_dict()
    fields = {
        key: value
        for key, value in payload.items()
        if key not in {"kind", "operation"} and value not in (None, False)
    }
    get_logger().log(
        level,
        "%s %s %s",
        event.kind,
        event.operation or "-",
        json.dumps(fields, sort_keys=True),
        extra={"event": payload},
    )
