"""Deterministic office runner.

The planner completion is loaded from the model (the recorded cassette by
default) and persisted before any tool runs. The interpreter walks that plan.
Tool text is an observation for the extractor, which has no tools and cannot
append a step. A mutating step restarts from argument resolution on resume;
the outbox write is taken once per tool-call id.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .approval import (
    DEFAULT_ELICITATION_SCHEMA,
    CheckpointStore,
    validate_elicitation_schema,
    validate_resume,
)
from .cassette import Cassette, finish_view, planner_view
from .envelopes import FINISH_SCHEMA, INPUT_SCHEMAS, OUTPUT_SCHEMAS, PLAN_SCHEMA, REFUSAL_SCHEMA
from .errors import CassetteMiss, LabError
from .grounding import ground_claims
from .policy import Bound, allow_send
from .redact import lab_logger
from .schema_dialect import validate_instance
from .tools import call_tool, extract_fields, is_mutating
from .trace import Tracer, arguments_hash, validate_span
from .world import World

LOGGER = lab_logger()

# capability is the defended runner. skip_policy is the undefended baseline and
# refuse_all is the utility-collapse baseline; both exist only for scoring.
DEFENSES = frozenset({"capability", "skip_policy", "refuse_all"})


class Model(Protocol):
    """Provider seam. The cassette implements it; a live adapter would too."""

    def complete(self, kind: str, view: dict[str, Any]) -> dict[str, Any]: ...


class RecordingSleeper:
    """Records backoff delays and does not sleep."""

    def __init__(self) -> None:
        self.delays: list[int] = []

    def sleep(self, seconds: int) -> None:
        self.delays.append(seconds)


@dataclass
class RunResult:
    status: str
    thread_id: str
    world_hash: str
    outbox: list[dict[str, str]]
    spans: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    error_type: str | None
    plan_tools: list[str]
    executed_tools: list[str]
    control_flow_taint: bool
    delays: list[int]
    pause_payload: dict[str, Any] | None
    body_hashes: dict[str, str]


@dataclass
class Session:
    task: dict[str, Any]
    world: World
    tracer: Tracer
    thread_id: str
    attack: bool
    attack_mode: str
    defense: str
    format_assertion: bool
    elicitation_schema: dict[str, Any]
    sleeper: RecordingSleeper
    plan: dict[str, Any] | None = None
    plan_tools: list[str] = field(default_factory=list)
    bindings: dict[str, dict[str, Bound]] = field(default_factory=dict)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"
    error_type: str | None = None
    claims: list[dict[str, Any]] = field(default_factory=list)
    taint: bool = False
    suppress_mutations: bool = False
    pause_index: int | None = None
    pause_schema: dict[str, Any] | None = None
    pause_payload: dict[str, Any] | None = None
    checkpointed_calls: set[str] = field(default_factory=set)
    checkpoint_record: dict[str, Any] | None = None
    committed_index: int | None = None
    verified: bool = False


def _pairs(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"document_id": row["document_id"], "recipient": row["recipient"]} for row in rows]


def _sent_result(row: dict[str, str]) -> dict[str, Any]:
    return {
        "isError": False,
        "content": [{"type": "text", "text": "sent"}],
        "structuredContent": {
            "document_id": row["document_id"],
            "recipient": row["recipient"],
            "outbox_id": row["outbox_id"],
            "status": "sent",
        },
        "error_class": None,
    }


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "isError": result["isError"],
        "content": result["content"],
        "structuredContent": result["structuredContent"],
    }


class OfficeRunner:
    def __init__(
        self,
        snapshot: dict[str, Any],
        tasks: list[dict[str, Any]],
        *,
        checkpoint_path: Path | None = None,
        defense: str = "capability",
        format_assertion: bool = True,
        external_annotations: dict[str, dict[str, Any]] | None = None,
        elicitation_schema: dict[str, Any] | None = None,
        model: Model | None = None,
    ) -> None:
        if defense not in DEFENSES:
            raise ValueError(f"unknown defense: {defense}")
        self.snapshot = snapshot
        self.model: Model = model if model is not None else Cassette(tasks)
        self.store = CheckpointStore(checkpoint_path)
        self.defense = defense
        self.format_assertion = format_assertion
        self.external_annotations = external_annotations or {}
        self.elicitation_schema = elicitation_schema or DEFAULT_ELICITATION_SCHEMA
        self.sessions: dict[str, Session] = {}

    def start(
        self,
        task: dict[str, Any],
        *,
        thread_id: str | None = None,
        attack: bool = False,
        attack_mode: str = "text",
        trial_index: int = 0,
        fault_trials: set[int] | None = None,
        transient_failures: int = 0,
        corrupt_read_output: bool = False,
    ) -> RunResult:
        thread = thread_id or f"thread-{task['id']}-{attack_mode}-t{trial_index}"
        world = World(self.snapshot)
        world.transient_remaining = transient_failures
        world.corrupt_read_output = corrupt_read_output
        tracer = Tracer(thread, world.known_bodies(attack))
        tracer.note_bodies(world.body_hashes())
        session = Session(
            task=task,
            world=world,
            tracer=tracer,
            thread_id=thread,
            attack=attack,
            attack_mode=attack_mode,
            defense=self.defense,
            format_assertion=self.format_assertion,
            elicitation_schema=self.elicitation_schema,
            sleeper=RecordingSleeper(),
        )
        self.sessions[thread] = session
        tracer.open_roots()
        faults = fault_trials or set()
        try:
            if trial_index in faults:
                completion: dict[str, Any] = {"kind": "refusal", "reason": "fault_trial"}
            else:
                completion = self.model.complete("plan", planner_view(task))
        except CassetteMiss:
            return self._close(session, "cassette_miss", "error")
        if not isinstance(completion, dict):
            return self._close(session, "schema_rejected", "error")
        if completion.get("kind") == "refusal":
            return self._refusal(session, completion)
        errors = validate_instance(PLAN_SCHEMA, completion, format_assertion=False)
        step_ids = [step["step_id"] for step in completion["steps"]] if not errors else []
        if errors or len(step_ids) != len(set(step_ids)):
            # Duplicate step ids would share a call id and hide a second send.
            return self._close(session, "schema_rejected", "error")
        session.plan = completion
        session.plan_tools = [step["tool"] for step in completion["steps"]]
        tracer.child(
            "plan",
            "plan",
            **{
                "lab.span.role": "plan",
                "lab.plan.tools": list(session.plan_tools),
                "gen_ai.output.messages": [
                    {"role": "assistant", "parts": [{"type": "text", "content": "plan"}]}
                ],
            },
        )
        LOGGER.info("event=plan_persisted thread_id=%s steps=%s", thread, len(session.plan_tools))
        return self._drive(session, 0, None)

    def resume(self, thread_id: str, decision: Any) -> RunResult:
        session = self.sessions.get(thread_id)
        if session is None:
            raise LabError(f"unknown thread: {thread_id}")
        schema = session.pause_schema or DEFAULT_ELICITATION_SCHEMA
        error = validate_resume(decision, schema)
        if error:
            return self._result(session, status="rejected", error_type=error)
        if session.status == "paused":
            record = self.store.latest(thread_id)
            if record is None:
                return self._result(session, status="rejected", error_type="no_checkpoint")
            expected_call = (session.pause_payload or {}).get("call_id")
            if record.get("step_index") != session.pause_index or record.get("call_id") != expected_call:
                return self._result(session, status="rejected", error_type="checkpoint_mismatch")
            session.status = "running"
            return self._drive(session, int(record["step_index"]), decision)
        if session.status == "finished" and session.committed_index is not None and decision["action"] == "accept":
            # A duplicate delivery of the approval. The ledger already holds the
            # row for this call id, so this records a replay and writes nothing.
            self._replay_committed(session)
            return self._result(session, status="finished", error_type=session.error_type)
        # Cancelled, declined, refused, and errored threads cannot be revived.
        return self._result(session, status="rejected", error_type="nothing_to_resume")

    def _replay_committed(self, session: Session) -> None:
        assert session.plan is not None and session.committed_index is not None
        step = session.plan["steps"][session.committed_index]
        call_id = f"call-{session.thread_id}-{step['step_id']}"
        row = session.world.find_outbox(call_id)
        if row is None:
            return
        arguments = {"document_id": row["document_id"], "recipient": row["recipient"]}
        self._emit_execute(
            session, "send_document", call_id, arguments, _sent_result(row), [], True, ledger_replay=True
        )

    def _drive(self, session: Session, start_index: int, decision: dict[str, Any] | None) -> RunResult:
        assert session.plan is not None
        steps = session.plan["steps"]
        incoming = decision
        index = start_index
        while index < len(steps):
            outcome = self._run_step(session, index, incoming)
            incoming = None
            if outcome == "paused":
                session.status = "paused"
                session.pause_index = index
                record = session.checkpoint_record
                # One record per paused call. A blocked edit re-pauses the same
                # call and leaves the file bytes as they were.
                if record is not None and record["call_id"] not in session.checkpointed_calls:
                    self.store.append(record)
                    session.checkpointed_calls.add(record["call_id"])
                return self._result(session, status="paused", error_type=None)
            if outcome == "cancelled":
                return self._close(session, "approval_cancelled", "cancelled")
            if outcome == "declined_stop":
                return self._close(session, None, "declined")
            if outcome == "error":
                return self._close(session, session.error_type or "_OTHER", "error")
            index += 1
        return self._finish(session)

    def _refusal(self, session: Session, completion: dict[str, Any]) -> RunResult:
        errors = validate_instance(REFUSAL_SCHEMA, completion, format_assertion=False)
        if errors:
            return self._close(session, "schema_rejected", "error")
        session.tracer.child(
            "chat",
            "chat refusal",
            **{
                "lab.span.role": "refusal",
                "error.type": "refusal",
                "gen_ai.output.messages": [
                    {
                        "role": "assistant",
                        "parts": [{"type": "refusal", "content": completion["reason"]}],
                    }
                ],
            },
        )
        return self._close(session, None, "refused")

    def _finish(self, session: Session) -> RunResult:
        try:
            finish = self.model.complete("finish", finish_view(session.task))
        except CassetteMiss:
            return self._close(session, "cassette_miss", "error")
        if not isinstance(finish, dict):
            return self._close(session, "schema_rejected", "error")
        errors = validate_instance(FINISH_SCHEMA, finish, format_assertion=False)
        if errors:
            return self._close(session, "schema_rejected", "error")
        session.claims = ground_claims(finish["claims"], session.tool_results)
        session.tracer.child(
            "chat",
            "chat finish",
            **{
                "lab.span.role": "finish",
                "lab.claims": session.claims,
                "gen_ai.output.messages": [
                    {"role": "assistant", "parts": [{"type": "text", "content": "finish"}]}
                ],
            },
        )
        return self._close(session, None, "finished")

    def _close(self, session: Session, error_type: str | None, status: str) -> RunResult:
        session.status = status
        if error_type:
            session.error_type = error_type
            session.tracer.mark_failure(error_type)
        self._verify(session)
        return self._result(session, status=status, error_type=session.error_type)

    def _verify(self, session: Session) -> None:
        if session.verified:
            return
        matched = _pairs(session.world.outbox) == _pairs(session.task["golden_outbox"])
        session.tracer.child(
            "chat",
            "chat verification",
            **{
                "lab.span.role": "verification",
                "lab.verification.passed": matched,
            },
        )
        session.verified = True

    def _result(self, session: Session, *, status: str, error_type: str | None) -> RunResult:
        spans = copy.deepcopy(session.tracer.spans)
        for span in spans:
            problems = validate_span(span)
            if problems:
                raise RuntimeError("span contract failed: " + ",".join(problems))
        executed = [
            span["attributes"]["gen_ai.tool.name"]
            for span in spans
            if span["attributes"]["gen_ai.operation.name"] == "execute_tool"
            and not span["attributes"].get("lab.ledger.replay")
            and not span["attributes"].get("lab.idempotent_retry")
        ]
        taint = any(span["attributes"].get("lab.control_flow_taint") for span in spans)
        return RunResult(
            status=status,
            thread_id=session.thread_id,
            world_hash=session.world.world_hash(),
            outbox=copy.deepcopy(session.world.outbox),
            spans=spans,
            claims=copy.deepcopy(session.claims),
            error_type=error_type,
            plan_tools=list(session.plan_tools),
            executed_tools=executed,
            control_flow_taint=taint,
            delays=list(session.sleeper.delays),
            pause_payload=copy.deepcopy(session.pause_payload),
            body_hashes=dict(session.tracer.body_hashes),
        )

    def _run_step(self, session: Session, index: int, decision: dict[str, Any] | None) -> str:
        assert session.plan is not None
        step = session.plan["steps"][index]
        tool_name = step["tool"]
        LOGGER.info(
            "event=step_start thread_id=%s step_id=%s tool=%s",
            session.thread_id,
            step["step_id"],
            tool_name,
        )
        predicate = self._predicate_state(session, step)
        if predicate == "unbound":
            session.error_type = "unbound_argument"
            return "error"
        if predicate == "skip":
            return "ok"
        if predicate == "taint":
            session.taint = True
            session.tracer.child(
                "chat",
                "chat control_flow",
                **{
                    "lab.span.role": "control_flow",
                    "lab.control_flow_taint": True,
                    "gen_ai.output.messages": [],
                },
            )
        if tool_name not in INPUT_SCHEMAS:
            session.error_type = "unknown_tool"
            return "error"
        if is_mutating(tool_name, self.external_annotations.get(tool_name)) and session.suppress_mutations:
            return "ok"
        if is_mutating(tool_name, self.external_annotations.get(tool_name)):
            return self._mutate(session, index, step, decision)
        return self._read(session, step)

    def _predicate_state(self, session: Session, step: dict[str, Any]) -> str:
        slot = step["predicate"]
        if slot is None:
            return "run"
        bound = self._resolve_slot(session, slot)
        if bound is None:
            return "unbound"
        if not bound.trusted:
            return "taint"
        if bound.value is None or str(bound.value).lower() in {"false", "0", ""}:
            return "skip"
        return "run"

    def _resolve_slot(self, session: Session, slot: dict[str, Any]) -> Bound | None:
        if slot["source"] == "literal":
            return Bound(slot["value"], True, ("user",), ())
        found = session.bindings.get(slot["from_step"] or "", {}).get(slot["field"] or "")
        return found

    def _concrete(self, session: Session, step: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Bound], str | None]:
        concrete: dict[str, Any] = {}
        bounds: dict[str, Bound] = {}
        for key, slot in step["arguments"].items():
            if slot is None:
                continue
            bound = self._resolve_slot(session, slot)
            if bound is None or bound.value is None:
                return {}, {}, "unbound_argument"
            concrete[key] = bound.value
            bounds[key] = bound
        return concrete, bounds, None

    def _read(self, session: Session, step: dict[str, Any]) -> str:
        concrete, _bounds, error = self._concrete(session, step)
        if error:
            session.error_type = error
            return "error"
        schema_errors = validate_instance(
            INPUT_SCHEMAS[step["tool"]], concrete, format_assertion=session.format_assertion
        )
        if schema_errors:
            session.error_type = "schema_invalid"
            return "error"
        call_id = f"call-{session.thread_id}-{step['step_id']}"
        result, delays = self._call_with_retry(session, step["tool"], concrete, call_id)
        if not result["isError"] and result["structuredContent"] is not None:
            output_errors = validate_instance(
                OUTPUT_SCHEMAS[step["tool"]],
                result["structuredContent"],
                format_assertion=False,
            )
            if output_errors:
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": "schema_invalid"}],
                    "structuredContent": None,
                    "error_class": "schema_invalid",
                }
        self._emit_execute(session, step["tool"], call_id, concrete, result, delays, policy_allowed=None)
        if result["isError"]:
            return "ok"
        if step["tool"] == "read_document" and result["structuredContent"].get("document_id") != concrete["document_id"]:
            return "ok"
        extraction = extract_fields(result["structuredContent"])
        if step["tool"] == "ask_user":
            trusted = True
            sources = ("user",)
            readers: tuple[str, ...] = ()
        elif step["tool"] == "read_document":
            document = session.world.documents[concrete["document_id"]]
            trusted = False
            sources = (call_id,)
            readers = tuple(document["readers"])
        else:
            trusted = False
            sources = (call_id,)
            readers = ()
        for name, value in extraction["fields"].items():
            session.bindings.setdefault(step["step_id"], {})[name] = Bound(value, trusted, sources, readers)
        session.tool_results.append({"step_id": step["step_id"], "call_id": call_id, "fields": dict(extraction["fields"])})
        session.tracer.child(
            "chat",
            "chat extract",
            **{
                "lab.span.role": "extract",
                "lab.extract.trusted": trusted,
                "lab.capability.reader_count": len(readers),
                "lab.capability.sources": list(sources),
                "lab.extract.fields": extraction["fields"],
            },
        )
        return "ok"

    def _call_with_retry(
        self, session: Session, tool_name: str, arguments: dict[str, Any], call_id: str
    ) -> tuple[dict[str, Any], list[int]]:
        """Retry transient errors. Each retried attempt gets its own execute_tool
        span marked lab.idempotent_retry, so the lints can tell it from a repeat."""
        delays: list[int] = []
        result: dict[str, Any] | None = None
        for attempt in range(1, 4):
            result = call_tool(
                tool_name,
                arguments,
                session.world,
                attack=session.attack,
                attack_mode=session.attack_mode,
                replies=session.task.get("user_replies") or {},
            )
            if result["isError"] and result["error_class"] == "transient" and attempt < 3:
                self._emit_execute(
                    session, tool_name, call_id, arguments, result, [], None, idempotent_retry=True
                )
                delay = attempt
                session.sleeper.sleep(delay)
                delays.append(delay)
                LOGGER.info(
                    "event=tool_retry thread_id=%s tool=%s attempt=%s delay=%s",
                    session.thread_id,
                    tool_name,
                    attempt,
                    delay,
                )
                continue
            break
        assert result is not None
        return result, delays

    def _mutate(self, session: Session, index: int, step: dict[str, Any], decision: dict[str, Any] | None) -> str:
        concrete, bounds, error = self._concrete(session, step)
        if error:
            session.error_type = error
            return "error"
        if decision is not None and decision.get("action") == "accept":
            concrete, bounds = self._apply_edit(session, concrete, bounds, decision["content"])
        schema_errors = validate_instance(
            INPUT_SCHEMAS["send_document"], concrete, format_assertion=session.format_assertion
        )
        if schema_errors:
            session.error_type = "schema_invalid"
            return "error"
        document_id = str(concrete["document_id"])
        recipient = str(concrete["recipient"])
        recipient_bound = bounds["recipient"]
        allowed, reason = allow_send(document_id, recipient, recipient_bound, session.world.documents)
        if session.taint and allowed:
            reason = "control_flow_taint"
        call_id = f"call-{session.thread_id}-{step['step_id']}"
        if session.defense == "refuse_all":
            self._policy_span(session, call_id, "deny", "refuse_all", False)
            return "ok"
        if session.defense == "skip_policy":
            self._policy_span(session, call_id, "allow" if allowed else "deny", reason, False)
            self._apply_once(session, call_id, document_id, recipient, concrete, policy_allowed=allowed)
            session.committed_index = index
            return "ok"
        action = decision.get("action") if decision is not None else None
        if action == "decline":
            self._approval_span(session, "decline", call_id, reason)
            return self._decline(session, index)
        if action == "cancel":
            self._approval_span(session, "cancel", call_id, reason)
            session.error_type = "approval_cancelled"
            return "cancelled"
        if action == "accept":
            if not allowed:
                self._policy_span(session, call_id, "deny", reason, False)
                self._pause(session, index, step, call_id, concrete, recipient_bound, reason)
                return "paused"
            self._policy_span(session, call_id, "allow", reason, False)
            self._approval_span(session, "accept", call_id, reason)
            self._apply_once(session, call_id, document_id, recipient, concrete, policy_allowed=True)
            session.committed_index = index
            return "ok"
        if session.taint or not allowed:
            return self._pause(session, index, step, call_id, concrete, recipient_bound, reason)
        self._policy_span(session, call_id, "allow", reason, True)
        self._apply_once(session, call_id, document_id, recipient, concrete, policy_allowed=True)
        session.committed_index = index
        return "ok"

    def _apply_edit(
        self,
        session: Session,
        concrete: dict[str, Any],
        bounds: dict[str, Bound],
        content: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Bound]]:
        updated_args = dict(concrete)
        updated_bounds = dict(bounds)
        if "recipient" in content:
            document = session.world.documents.get(str(concrete.get("document_id")))
            readers = tuple(document["readers"]) if document else ()
            updated_args["recipient"] = content["recipient"]
            updated_bounds["recipient"] = Bound(content["recipient"], False, ("approval",), readers)
        return updated_args, updated_bounds

    def _decline(self, session: Session, index: int) -> str:
        assert session.plan is not None
        later = session.plan["steps"][index + 1 :]
        if any(not is_mutating(step["tool"], self.external_annotations.get(step["tool"])) for step in later):
            session.suppress_mutations = True
            return "ok"
        return "declined_stop"

    def _pause(
        self,
        session: Session,
        index: int,
        step: dict[str, Any],
        call_id: str,
        arguments: dict[str, Any],
        recipient_bound: Bound,
        reason: str,
    ) -> str:
        schema_error = validate_elicitation_schema(session.elicitation_schema)
        if schema_error:
            session.error_type = schema_error
            return "error"
        payload = {
            "method": "elicitation/create",
            "params": {
                "message": "Approve the send or provide a replacement recipient.",
                "requestedSchema": session.elicitation_schema,
            },
            "tool_name": "send_document",
            "arguments": arguments,
            "capability_sources": list(recipient_bound.sources),
            "policy_reason": reason,
            "call_id": call_id,
        }
        session.pause_payload = payload
        session.pause_schema = session.elicitation_schema
        session.pause_index = index
        session.checkpoint_record = {
            "thread_id": session.thread_id,
            "task_id": session.task["id"],
            "step_id": step["step_id"],
            "step_index": index,
            "call_id": call_id,
            "status": "paused",
            "policy_reason": reason,
            "pause_payload": payload,
        }
        self._approval_span(session, None, call_id, reason)
        return "paused"

    def _policy_span(self, session: Session, call_id: str, decision: str, reason: str, preapproved: bool) -> None:
        session.tracer.child(
            "policy_check",
            "policy_check send_document",
            **{
                "lab.span.role": "policy",
                "lab.policy.decision": decision,
                "lab.policy.reason": reason,
                "lab.policy.preapproved": preapproved,
                "gen_ai.tool.name": "send_document",
                "gen_ai.tool.call.id": call_id,
            },
        )

    def _approval_span(self, session: Session, action: str | None, call_id: str, reason: str) -> None:
        session.tracer.child(
            "request_approval",
            "request_approval send_document",
            **{
                "lab.span.role": "approval",
                "lab.approval.action": action,
                "lab.policy.reason": reason,
                "gen_ai.tool.name": "send_document",
                "gen_ai.tool.call.id": call_id,
            },
        )

    def _apply_once(
        self,
        session: Session,
        call_id: str,
        document_id: str,
        recipient: str,
        arguments: dict[str, Any],
        *,
        policy_allowed: bool,
    ) -> None:
        existing = session.world.find_outbox(call_id)
        if existing is not None:
            self._emit_execute(
                session, "send_document", call_id, arguments, _sent_result(existing), [], policy_allowed,
                ledger_replay=True,
            )
            return
        if document_id not in session.world.documents:
            result = {
                "isError": True,
                "content": [{"type": "text", "text": "unknown document"}],
                "structuredContent": None,
                "error_class": "unknown_document",
            }
            self._emit_execute(session, "send_document", call_id, arguments, result, [], policy_allowed)
            return
        row = session.world.send(document_id, recipient, call_id)
        self._emit_execute(session, "send_document", call_id, arguments, _sent_result(row), [], policy_allowed)
        session.tool_results.append(
            {
                "step_id": call_id,
                "call_id": call_id,
                "fields": {
                    "document_id": row["document_id"],
                    "named_recipient": None,
                    "title": None,
                    "urgent": None,
                    "answer": None,
                    "recipient": row["recipient"],
                    "outbox_id": row["outbox_id"],
                },
            }
        )

    def _emit_execute(
        self,
        session: Session,
        tool_name: str,
        call_id: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        delays: list[int],
        policy_allowed: bool | None,
        *,
        ledger_replay: bool = False,
        idempotent_retry: bool = False,
    ) -> None:
        session.tracer.child(
            "execute_tool",
            f"execute_tool {tool_name}",
            **{
                "gen_ai.tool.name": tool_name,
                "gen_ai.tool.call.id": call_id,
                "error.type": result["error_class"] if result["isError"] else None,
                "lab.tool.mutates": is_mutating(tool_name, self.external_annotations.get(tool_name)),
                "lab.tool.arguments_hash": arguments_hash(arguments),
                "lab.tool.arguments": arguments,
                "lab.ledger.replay": ledger_replay,
                "lab.idempotent_retry": idempotent_retry,
                "lab.retry.delays": delays,
                "lab.policy.allowed": policy_allowed,
                "gen_ai.input.messages": [
                    {
                        "role": "assistant",
                        "parts": [
                            {
                                "type": "tool_call",
                                "id": call_id,
                                "name": tool_name,
                                "arguments": arguments,
                            }
                        ],
                    }
                ],
                "gen_ai.output.messages": [
                    {
                        "role": "tool",
                        "parts": [
                            {
                                "type": "tool_call_response",
                                "id": call_id,
                                "name": tool_name,
                                "response": _public_result(result),
                            }
                        ],
                    }
                ],
            },
        )
