"""Maintenance log. Failure and repair lines carry locations, not field text."""

from __future__ import annotations

import logging

from .model import ParseFailure, ParseSuccess, RepairEvent

LOG = logging.getLogger("wharf_sheet")
LOG.addHandler(logging.NullHandler())


def _cell(value: int | None) -> str:
    if value is None:
        return "none"
    return str(value)


def log_failure(failure: ParseFailure) -> None:
    LOG.warning(
        "code=%s decision=%s record=%s field=%s byte=%s bom_stripped=%s",
        failure.code,
        failure.decision_id,
        _cell(failure.record_index),
        _cell(failure.field_index),
        _cell(failure.byte_offset),
        str(failure.bom_stripped).lower(),
    )


def log_event(event: RepairEvent) -> None:
    LOG.info(
        "repair decision=%s record=%s field=%s byte=%s",
        event.decision_id,
        event.record_index,
        event.field_index,
        event.byte_offset,
    )


def log_result(result: ParseSuccess | ParseFailure) -> None:
    if isinstance(result, ParseFailure):
        log_failure(result)
        for event in result.events:
            log_event(event)
        return
    for event in result.events:
        log_event(event)
