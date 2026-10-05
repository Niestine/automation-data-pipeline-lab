"""In-process gauge-bulletin client.

Responses are supplied by the caller. This module does not open a socket, and
it does not fetch ``Link`` targets. A deprecated operation stays callable: the
call returns the business object and records a warning. The call raises for an
illegal header, for a schema failure under the active reader, or for an
operation that was removed with no earlier deprecation mark. ``phase`` is the
header clock; a description-channel warning leaves it empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from contract_lab.changes import METHODS
from contract_lab.errors import (
    OperationNotFound,
    OperationRemoved,
    SchemaRejected,
    UnmanagedRemoval,
)
from contract_lab.events import Event
from contract_lab.http_lifecycle import format_instant, interpret_headers
from contract_lab.logsetup import log_event
from contract_lab.readers import as_strict, as_tolerant, project, validate
from contract_lab.retirement import assess, deprecated_marks


@dataclass
class ScriptedResponse:
    status: int | None = None
    headers: dict[str, str] | None = None
    body: object = None
    transport_error: str | None = None
    url: str = "https://gauge.example.test/gauges/g-1"
    used_base: str | None = None

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {}


def coerce_response(value: ScriptedResponse | dict) -> ScriptedResponse:
    if isinstance(value, ScriptedResponse):
        return value
    return ScriptedResponse(
        status=value.get("status"),
        headers=value.get("headers"),
        body=value.get("body"),
        transport_error=value.get("transport_error"),
        url=value.get("url", "https://gauge.example.test/gauges/g-1"),
        used_base=value.get("used_base"),
    )


@dataclass
class CallResult:
    business: object
    events: list[Event]
    reader_results: dict
    phase: str
    stability_assured: bool
    status: int | None
    transport_error: str | None = None


def _resolve(document: dict, method: str, path: str) -> dict | None:
    item = (document.get("paths") or {}).get(path)
    if not isinstance(item, dict):
        return None
    for key, operation in item.items():
        if key.lower() == method.lower() and key.lower() in METHODS and isinstance(operation, dict):
            return operation
    return None


def _response_schema(operation: dict, status: int):
    """The JSON schema documented for this exact status code, if any."""

    responses = operation.get("responses") or {}
    declared = responses.get(str(status)) or {}
    content = declared.get("content") or {}
    media = content.get("application/json") or {}
    schema = media.get("schema")
    return schema if isinstance(schema, dict) else None


class ContractClient:
    def __init__(
        self,
        *,
        reader: str = "tolerant",
        now: datetime | None = None,
        policy_map: dict[str, list[str]] | None = None,
        replacement_base: str | None = None,
    ):
        if reader not in {"strict", "tolerant"}:
            raise ValueError("reader must be strict or tolerant")
        self.reader = reader
        self.now = now or datetime(2024, 6, 1, tzinfo=timezone.utc)
        if self.now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self.policy_map = {key: list(values) for key, values in (policy_map or {}).items()}
        self.replacement_base = replacement_base
        self.document: dict | None = None
        self.history: list[dict] = []
        self._observed: dict[str, object] = {}

    def load(self, document: dict, history: list[dict] | None = None) -> None:
        self.document = document
        self.history = list(history) if history is not None else [document]

    def call(self, method: str, path: str, response: ScriptedResponse | dict) -> CallResult:
        if self.document is None:
            raise OperationNotFound(f"{method.upper()} {path}")
        script = coerce_response(response)
        label = f"{method.upper()} {path}"
        operation = _resolve(self.document, method, path)
        if operation is None:
            self._raise_missing(label)
        assert operation is not None
        events: list[Event] = []
        for mark in deprecated_marks(self.document, method, path):
            events.append(
                Event(
                    kind="deprecation_warning",
                    channel="description",
                    operation=label,
                    element_pointer=mark.pointer,
                    replacement=mark.replacement,
                )
            )
        facts = interpret_headers(script.headers, self.now)
        self._observed[script.url] = facts
        if facts.policy_targets and facts.deprecation_at is None:
            for target in facts.policy_targets:
                events.append(
                    Event(
                        kind="policy_link",
                        operation=label,
                        phase="not_deprecated",
                        target=target,
                        scope_uri=script.url,
                        applied_uri=script.url,
                    )
                )
        if facts.deprecation_at is not None:
            events.append(self._header_warning(label, script.url, script.url, facts, inherited=False))
        else:
            for source, prior in self._inherited(script.url):
                events.append(self._header_warning(label, source, script.url, prior, inherited=True))
        if facts.clock_order_error and facts.deprecation_at and facts.sunset_at:
            events.append(
                Event(
                    kind="clock_order_error",
                    operation=label,
                    deprecation_at=format_instant(facts.deprecation_at),
                    sunset_at=format_instant(facts.sunset_at),
                    scope_uri=script.url,
                    detail="sunset earlier than deprecation",
                )
            )
        status = None if script.transport_error else script.status
        body = None if script.transport_error else script.body
        reader_results: dict = {}
        business = None
        # A redirect or error after Sunset is recorded, not validated against
        # the 200 schema. Only a status with its own documented schema is read.
        schema = _response_schema(operation, status) if isinstance(status, int) else None
        if schema is not None and status < 400:
            reader_results["strict"] = validate(as_strict(schema), body)
            reader_results["tolerant"] = validate(as_tolerant(schema), body)
            active = reader_results[self.reader]
            if not active["valid"]:
                node = active["errors"][0]
                events.append(
                    Event(
                        kind="schema_rejected",
                        operation=label,
                        reader=self.reader,
                        instance_location=node.get("instanceLocation"),
                    )
                )
                self._log(events)
                raise SchemaRejected(node, reader_results, events)
            business = project(schema, body)
        if facts.sunset_phase == "elapsed" and facts.sunset_at is not None:
            events.append(
                Event(
                    kind="sunset_elapsed",
                    operation=label,
                    status=status,
                    sunset_at=format_instant(facts.sunset_at),
                    transport_error=script.transport_error,
                    scope_uri=script.url,
                )
            )
            switched = (
                self.replacement_base is not None
                and script.used_base is not None
                and script.used_base == self.replacement_base
            )
            if status == 410 or switched:
                events.append(
                    Event(
                        kind="eol_transition",
                        operation=label,
                        status=status,
                        sunset_at=format_instant(facts.sunset_at),
                        scope_uri=script.url,
                        detail="status_410" if status == 410 else "base_switch",
                    )
                )
        phase = facts.phase
        stability = phase != "already_deprecated" and facts.sunset_phase != "elapsed"
        self._log(events)
        return CallResult(
            business=business,
            events=events,
            reader_results=reader_results,
            phase=phase,
            stability_assured=stability,
            status=status,
            transport_error=script.transport_error,
        )

    def _header_warning(self, label: str, scope: str, applied: str, facts, *, inherited: bool) -> Event:
        return Event(
            kind="deprecation_warning",
            channel="header",
            operation=label,
            element_pointer=scope,
            replacement=None,
            deprecation_at=format_instant(facts.deprecation_at) if facts.deprecation_at else None,
            sunset_at=format_instant(facts.sunset_at) if facts.sunset_at else None,
            phase=facts.phase,
            scope_uri=scope,
            applied_uri=applied,
            inherited=inherited,
        )

    def _inherited(self, url: str) -> list[tuple[str, object]]:
        found = []
        for source, targets in self.policy_map.items():
            prior = self._observed.get(source)
            if url in targets and prior is not None and getattr(prior, "deprecation_at", None) is not None:
                found.append((source, prior))
        return found

    def _raise_missing(self, label: str) -> None:
        report = assess(self.history)
        matches = [item for item in report.removals if item.kind == "operation" and item.operation == label]
        unmanaged = [item for item in matches if item.status == "unmanaged"]
        if unmanaged:
            raise UnmanagedRemoval(unmanaged)
        if matches:
            raise OperationRemoved(label)
        raise OperationNotFound(label)

    @staticmethod
    def _log(events: list[Event]) -> None:
        for event in events:
            log_event(event)
