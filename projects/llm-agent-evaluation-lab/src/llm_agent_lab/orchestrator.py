"""Agent pipeline: guardrails, structured completion, retries, tools, store."""

from __future__ import annotations

from typing import Any, Callable, Optional

from .contracts import ContractError, parse_agent_output, system_prompt
from .guardrails import approval_for, inspect_input, inspect_output
from .models import (
    CONTRACT_VERSION,
    SCHEMA_NAME,
    AgentOutput,
    Approval,
    CompletionRequest,
    RunResult,
    RunStatus,
    Ticket,
    ToolResult,
    TraceEvent,
)
from .provider import ProviderError
from .retry import RetryPolicy, backoff_ms, is_retryable, rng_for
from .store import RunStore
from .telemetry import JsonLogger, WallClock
from .tools import RecordStore, ToolExecutor


class AgentOrchestrator:
    def __init__(
        self,
        provider: Any,
        *,
        records: Optional[RecordStore] = None,
        store: Optional[RunStore] = None,
        policy: Optional[RetryPolicy] = None,
        logger: Optional[JsonLogger] = None,
        clock: Any = None,
        sleeper: Optional[Callable[[int], None]] = None,
        seed: int = 7,
    ) -> None:
        self.provider = provider
        self.records = records if records is not None else RecordStore()
        self.tools = ToolExecutor(self.records)
        self.store = store if store is not None else RunStore()
        self.policy = policy if policy is not None else RetryPolicy()
        self.logger = logger if logger is not None else JsonLogger()
        self.clock = clock if clock is not None else WallClock()
        self.sleeper = sleeper if sleeper is not None else self.clock.sleep
        self.seed = seed

    def run(
        self,
        ticket: Ticket,
        *,
        dry_run: bool = False,
        approve: bool = False,
        force: bool = False,
    ) -> RunResult:
        run_id = self.store.run_id(ticket, dry_run=dry_run, approve=approve)
        self.logger.log(
            "run_start",
            ticket_id=ticket.ticket_id,
            run_id=run_id,
            dry_run=dry_run,
            approve=approve,
        )
        if not force:
            cached = self.store.get(ticket, dry_run=dry_run, approve=approve)
            if cached is not None:
                self.logger.log("cache_hit", ticket_id=ticket.ticket_id, run_id=run_id)
                cached.trace.append(self._event("cache_hit", ticket_id=ticket.ticket_id))
                return cached

        trace: list[TraceEvent] = []
        input_violations = inspect_input(ticket)
        if input_violations:
            self.logger.log(
                "input_guardrail",
                ticket_id=ticket.ticket_id,
                codes=[item.code for item in input_violations],
            )
            trace.append(
                self._event(
                    "input_guardrail",
                    codes=[item.code for item in input_violations],
                )
            )
            result = self._result(
                ticket,
                run_id=run_id,
                status=RunStatus.BLOCKED,
                attempts=0,
                error_code=input_violations[0].code,
                trace=trace,
                dry_run=dry_run,
            )
            return self._persist(ticket, result, dry_run=dry_run, approve=approve)

        output, attempts, error_code, trace = self._complete_with_retries(ticket, trace)
        if output is None:
            result = self._result(
                ticket,
                run_id=run_id,
                status=RunStatus.FAILED,
                attempts=attempts,
                error_code=error_code,
                trace=trace,
                dry_run=dry_run,
            )
            return self._persist(ticket, result, dry_run=dry_run, approve=approve)

        output_violations = inspect_output(output, ticket)
        if output_violations:
            self.logger.log(
                "output_guardrail",
                ticket_id=ticket.ticket_id,
                codes=[item.code for item in output_violations],
            )
            trace.append(
                self._event(
                    "output_guardrail",
                    codes=[item.code for item in output_violations],
                )
            )
            result = self._result(
                ticket,
                run_id=run_id,
                status=RunStatus.BLOCKED,
                attempts=attempts,
                output=output,
                error_code=output_violations[0].code,
                trace=trace,
                dry_run=dry_run,
            )
            return self._persist(ticket, result, dry_run=dry_run, approve=approve)

        decision = approval_for(output, ticket)
        trace.append(self._event("approval", decision=decision, role=ticket.requester_role))
        self.logger.log("approval", ticket_id=ticket.ticket_id, decision=decision)

        if decision == Approval.DENY:
            tool_result = self.tools.execute(
                output.proposed_action,
                output.action_args,
                approval=Approval.DENY,
                dry_run=dry_run,
            )
            trace.append(self._event("tool", tool=tool_result.tool, status=tool_result.status, dry_run=dry_run))
            result = self._result(
                ticket,
                run_id=run_id,
                status=RunStatus.DENIED,
                attempts=attempts,
                output=output,
                approval=decision,
                tool_result=tool_result,
                error_code="policy_denied",
                trace=trace,
                dry_run=dry_run,
            )
            return self._persist(ticket, result, dry_run=dry_run, approve=approve)

        effective_approval = Approval.AUTO_ALLOW if (decision == Approval.AUTO_ALLOW or approve) else decision
        tool_result = self.tools.execute(
            output.proposed_action,
            output.action_args,
            approval=effective_approval,
            dry_run=dry_run,
        )
        trace.append(
            self._event(
                "tool",
                tool=tool_result.tool,
                status=tool_result.status,
                dry_run=tool_result.dry_run,
            )
        )
        status = self._status_from_tool(tool_result, dry_run=dry_run)
        error_code = None
        if tool_result.status == "denied":
            error_code = str(tool_result.payload.get("reason") or "policy_denied")
        elif tool_result.status == "failed":
            error_code = str(tool_result.payload.get("reason") or "tool_failed")
        result = self._result(
            ticket,
            run_id=run_id,
            status=status,
            attempts=attempts,
            output=output,
            approval=decision,
            tool_result=tool_result,
            error_code=error_code,
            trace=trace,
            dry_run=dry_run,
        )
        return self._persist(ticket, result, dry_run=dry_run, approve=approve)

    def _complete_with_retries(
        self,
        ticket: Ticket,
        trace: list[TraceEvent],
    ) -> tuple[Optional[AgentOutput], int, Optional[str], list[TraceEvent]]:
        rng = rng_for(ticket.ticket_id, self.seed)
        last_code: Optional[str] = None
        last_transient = False
        output: Optional[AgentOutput] = None
        attempts = 0
        for attempt in range(1, self.policy.max_attempts + 1):
            attempts = attempt
            request = CompletionRequest(
                task_id=ticket.ticket_id,
                system=system_prompt(),
                user=ticket.canonical_json(),
                response_schema_name=SCHEMA_NAME,
                response_format="json_object",
                temperature=0.0,
                attempt=attempt,
            )
            trace.append(self._event("attempt_start", attempt=attempt))
            self.logger.log("attempt_start", ticket_id=ticket.ticket_id, attempt=attempt)
            try:
                response = self.provider.complete(request)
            except ProviderError as exc:
                last_code = exc.code
                last_transient = exc.transient
                trace.append(
                    self._event(
                        "provider_error",
                        attempt=attempt,
                        code=exc.code,
                        transient=exc.transient,
                    )
                )
                self.logger.log(
                    "provider_error",
                    ticket_id=ticket.ticket_id,
                    attempt=attempt,
                    code=exc.code,
                    transient=exc.transient,
                )
            else:
                try:
                    output = parse_agent_output(response.text)
                except ContractError as exc:
                    last_code = exc.code
                    last_transient = False
                    trace.append(
                        self._event(
                            "contract_error",
                            attempt=attempt,
                            code=exc.code,
                            errors=exc.errors,
                        )
                    )
                    self.logger.log(
                        "contract_error",
                        ticket_id=ticket.ticket_id,
                        attempt=attempt,
                        code=exc.code,
                    )
                else:
                    return output, attempts, None, trace

            retryable = is_retryable(last_code or "provider_error", transient=last_transient, policy=self.policy)
            if retryable and attempt < self.policy.max_attempts:
                delay = backoff_ms(self.policy, attempt, rng)
                trace.append(self._event("retry", attempt=attempt, delay_ms=delay, code=last_code))
                self.logger.log("retry", ticket_id=ticket.ticket_id, attempt=attempt, delay_ms=delay)
                self.sleeper(delay)
                continue
            break
        return None, attempts, last_code, trace

    def _status_from_tool(self, tool_result: ToolResult, *, dry_run: bool) -> str:
        if dry_run and tool_result.status == "dry_run":
            return RunStatus.DRY_RUN
        if tool_result.status == "pending_approval":
            return RunStatus.PENDING_APPROVAL
        if tool_result.status == "denied":
            return RunStatus.DENIED
        if tool_result.status == "completed":
            return RunStatus.COMPLETED
        return RunStatus.FAILED

    def _event(self, event: str, **fields: Any) -> TraceEvent:
        return TraceEvent(at_ms=self.clock.now_ms(), event=event, fields=dict(fields))

    def _result(
        self,
        ticket: Ticket,
        *,
        run_id: str,
        status: str,
        attempts: int,
        trace: list[TraceEvent],
        dry_run: bool,
        output: Optional[AgentOutput] = None,
        approval: Optional[str] = None,
        tool_result: Optional[ToolResult] = None,
        error_code: Optional[str] = None,
    ) -> RunResult:
        result = RunResult(
            ticket_id=ticket.ticket_id,
            status=status,
            contract_version=CONTRACT_VERSION,
            input_hash=ticket.input_hash(),
            run_id=run_id,
            attempts=attempts,
            output=output,
            approval=approval,
            tool_result=tool_result,
            error_code=error_code,
            trace=trace,
            cached=False,
            dry_run=dry_run,
        )
        self.logger.log("run_end", ticket_id=ticket.ticket_id, status=status, attempts=attempts)
        result.trace.append(self._event("run_end", status=status, attempts=attempts))
        return result

    def _persist(
        self,
        ticket: Ticket,
        result: RunResult,
        *,
        dry_run: bool,
        approve: bool,
    ) -> RunResult:
        # Failed runs performed no side effects; caching them would turn a
        # transient outage into a permanent answer for this ticket.
        if result.status == RunStatus.FAILED:
            self.logger.log("not_cached", ticket_id=ticket.ticket_id, status=result.status)
            return result
        self.store.save(ticket, result, dry_run=dry_run, approve=approve)
        return result
