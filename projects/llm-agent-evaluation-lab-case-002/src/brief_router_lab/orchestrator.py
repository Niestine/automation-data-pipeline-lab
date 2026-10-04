"""Planner → router admission → bound execution with retries and a circuit breaker."""

from __future__ import annotations

from typing import Any, Callable, Optional
import copy

from .bindings import BindError, resolve_bind_map
from .contracts import ContractError, parse_tool_plan, system_prompt, validate_schema
from .models import (
    CONTRACT_VERSION,
    SCHEMA_NAME,
    Approval,
    CompletionRequest,
    PlanStep,
    RunResult,
    RunStatus,
    StepResult,
    ToolPlan,
    TraceEvent,
    WorkPacket,
)
from .policy import admit_plan, approval_for, inspect_input, inspect_plan_text, runtime_classification
from .provider import ProviderError
from .registry import get_tool
from .retry import CircuitBreaker, RetryPolicy, backoff_ms, is_retryable, rng_for
from .store import RunStore, StepStore, step_key
from .telemetry import JsonLogger, WallClock
from .workspace import BriefWorkspace, ToolError


class BriefRouter:
    def __init__(
        self,
        provider: Any,
        *,
        workspace: Optional[BriefWorkspace] = None,
        store: Optional[RunStore] = None,
        steps: Optional[StepStore] = None,
        policy: Optional[RetryPolicy] = None,
        breaker: Optional[CircuitBreaker] = None,
        logger: Optional[JsonLogger] = None,
        clock: Any = None,
        sleeper: Optional[Callable[[int], None]] = None,
        seed: int = 11,
    ) -> None:
        self.provider = provider
        self.workspace = workspace if workspace is not None else BriefWorkspace()
        self.store = store if store is not None else RunStore()
        self.steps = steps if steps is not None else StepStore()
        self.policy = policy if policy is not None else RetryPolicy()
        self.breaker = breaker if breaker is not None else CircuitBreaker()
        self.logger = logger if logger is not None else JsonLogger()
        self.clock = clock if clock is not None else WallClock()
        self.sleeper = sleeper if sleeper is not None else self.clock.sleep
        self.seed = seed

    def run(
        self,
        packet: WorkPacket,
        *,
        dry_run: bool = False,
        approve: bool = False,
        force: bool = False,
    ) -> RunResult:
        run_id = self.store.run_id(packet, dry_run=dry_run, approve=approve)
        self.logger.log(
            "run_start",
            packet_id=packet.packet_id,
            run_id=run_id,
            dry_run=dry_run,
            approve=approve,
        )
        if not force:
            cached = self.store.get(packet, dry_run=dry_run, approve=approve)
            if cached is not None:
                self.logger.log("cache_hit", packet_id=packet.packet_id, run_id=run_id)
                cached.trace.append(self._event("cache_hit", packet_id=packet.packet_id))
                return cached

        trace: list[TraceEvent] = []
        input_violations = inspect_input(packet)
        if input_violations:
            self.logger.log(
                "input_guardrail",
                packet_id=packet.packet_id,
                codes=[item.code for item in input_violations],
            )
            trace.append(self._event("input_guardrail", codes=[item.code for item in input_violations]))
            result = self._result(
                packet,
                run_id=run_id,
                status=RunStatus.BLOCKED,
                attempts=0,
                error_code=input_violations[0].code,
                trace=trace,
                dry_run=dry_run,
            )
            return self._persist(packet, result, dry_run=dry_run, approve=approve)

        plan, attempts, error_code, trace = self._plan_with_retries(packet, trace)
        if plan is None:
            result = self._result(
                packet,
                run_id=run_id,
                status=RunStatus.FAILED,
                attempts=attempts,
                error_code=error_code,
                trace=trace,
                dry_run=dry_run,
            )
            return self._persist(packet, result, dry_run=dry_run, approve=approve)

        output_violations = inspect_plan_text(plan)
        if output_violations:
            self.logger.log(
                "output_guardrail",
                packet_id=packet.packet_id,
                codes=[item.code for item in output_violations],
            )
            trace.append(self._event("output_guardrail", codes=[item.code for item in output_violations]))
            result = self._result(
                packet,
                run_id=run_id,
                status=RunStatus.BLOCKED,
                attempts=attempts,
                plan=plan,
                error_code=output_violations[0].code,
                tokens_planned=0,
                steps_planned=len(plan.steps),
                trace=trace,
                dry_run=dry_run,
            )
            return self._persist(packet, result, dry_run=dry_run, approve=approve)

        admission = admit_plan(plan, packet, self.workspace.classification)
        trace.append(
            self._event(
                "admission",
                ok=admission.ok,
                code=admission.code,
                tokens_planned=admission.tokens_planned,
            )
        )
        self.logger.log(
            "admission",
            packet_id=packet.packet_id,
            ok=admission.ok,
            code=admission.code,
        )
        if not admission.ok:
            result = self._result(
                packet,
                run_id=run_id,
                status=RunStatus.DENIED,
                attempts=attempts,
                plan=plan,
                error_code=admission.code,
                tokens_planned=admission.tokens_planned,
                steps_planned=len(plan.steps),
                trace=trace,
                dry_run=dry_run,
            )
            return self._persist(packet, result, dry_run=dry_run, approve=approve)

        decision = approval_for(plan)
        trace.append(self._event("approval", decision=decision, role=packet.operator_role))
        self.logger.log("approval", packet_id=packet.packet_id, decision=decision)
        if decision == Approval.REQUIRE_APPROVAL and not approve:
            result = self._result(
                packet,
                run_id=run_id,
                status=RunStatus.PENDING_APPROVAL,
                attempts=attempts,
                plan=plan,
                approval=decision,
                error_code="needs_approval",
                tokens_planned=admission.tokens_planned,
                steps_planned=len(plan.steps),
                trace=trace,
                dry_run=dry_run,
            )
            return self._persist(packet, result, dry_run=dry_run, approve=approve)

        step_results, exec_error, tokens_used, trace = self._execute_plan(
            packet,
            plan,
            trace,
            dry_run=dry_run,
        )
        if exec_error:
            status = RunStatus.FAILED
        elif dry_run:
            status = RunStatus.DRY_RUN
        else:
            status = RunStatus.COMPLETED
        result = self._result(
            packet,
            run_id=run_id,
            status=status,
            attempts=attempts,
            plan=plan,
            approval=decision,
            step_results=step_results,
            error_code=exec_error,
            tokens_planned=admission.tokens_planned,
            tokens_used=tokens_used,
            steps_planned=len(plan.steps),
            trace=trace,
            dry_run=dry_run,
        )
        return self._persist(packet, result, dry_run=dry_run, approve=approve)

    def _plan_with_retries(
        self,
        packet: WorkPacket,
        trace: list[TraceEvent],
    ) -> tuple[Optional[ToolPlan], int, Optional[str], list[TraceEvent]]:
        rng = rng_for(packet.packet_id, self.seed)
        last_code: Optional[str] = None
        last_transient = False
        plan: Optional[ToolPlan] = None
        attempts = 0
        for attempt in range(1, self.policy.max_attempts + 1):
            attempts = attempt
            request = CompletionRequest(
                task_id=packet.packet_id,
                system=system_prompt(),
                user=packet.canonical_json(),
                response_schema_name=SCHEMA_NAME,
                response_format="json_object",
                temperature=0.0,
                attempt=attempt,
            )
            trace.append(self._event("attempt_start", attempt=attempt))
            self.logger.log("attempt_start", packet_id=packet.packet_id, attempt=attempt)
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
                    packet_id=packet.packet_id,
                    attempt=attempt,
                    code=exc.code,
                    transient=exc.transient,
                )
            else:
                try:
                    plan = parse_tool_plan(response.text)
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
                        packet_id=packet.packet_id,
                        attempt=attempt,
                        code=exc.code,
                    )
                else:
                    return plan, attempts, None, trace

            retryable = is_retryable(last_code or "provider_error", transient=last_transient, policy=self.policy)
            if retryable and attempt < self.policy.max_attempts:
                delay = backoff_ms(self.policy, attempt, rng)
                trace.append(self._event("retry", attempt=attempt, delay_ms=delay, code=last_code))
                self.logger.log("retry", packet_id=packet.packet_id, attempt=attempt, delay_ms=delay)
                self.sleeper(delay)
                continue
            break
        return None, attempts, last_code, trace

    def _execute_plan(
        self,
        packet: WorkPacket,
        plan: ToolPlan,
        trace: list[TraceEvent],
        *,
        dry_run: bool,
    ) -> tuple[list[StepResult], Optional[str], int, list[TraceEvent]]:
        completed: dict[str, Any] = {}
        results: list[StepResult] = []
        tokens_used = 0
        rng = rng_for(f"{packet.packet_id}:tools", self.seed)
        for step in plan.steps:
            spec = get_tool(step.tool)
            cost = 0 if spec is None else spec.token_cost
            if not self.breaker.allow(step.tool, self.clock.now_ms()):
                self.logger.log("circuit_open", packet_id=packet.packet_id, tool=step.tool)
                trace.append(self._event("circuit_open", tool=step.tool, step_id=step.id))
                return results, "circuit_open", tokens_used, trace
            try:
                bound = resolve_bind_map(step.bind, completed)
            except BindError as exc:
                trace.append(self._event("bind_error", step_id=step.id, code=exc.code))
                self.logger.log("bind_error", packet_id=packet.packet_id, step_id=step.id, code=exc.code)
                return results, exc.code, tokens_used, trace
            merged = dict(step.args)
            merged.update(bound)
            if spec is not None:
                schema_errors = validate_schema(spec.arg_schema, merged, f"$.{step.id}")
                if schema_errors:
                    trace.append(self._event("tool_schema", step_id=step.id, errors=schema_errors))
                    return results, "tool_schema", tokens_used, trace
            class_hit = runtime_classification(step.tool, merged, packet, self.workspace.classification)
            if class_hit:
                trace.append(self._event("runtime_policy", step_id=step.id, code=class_hit.code))
                return results, class_hit.code, tokens_used, trace

            key = step_key(packet, step.id, merged, dry_run=dry_run)
            cached_step = self.steps.get(key)
            if cached_step is not None and cached_step.status == "completed":
                replayed = copy.deepcopy(cached_step)
                replayed.replayed = True
                results.append(replayed)
                completed[step.id] = replayed.payload
                tokens_used += cost
                trace.append(self._event("step_replay", step_id=step.id, tool=step.tool))
                continue

            step_result, error = self._invoke_with_retries(packet, step, merged, rng, dry_run=dry_run)
            results.append(step_result)
            trace.append(
                self._event(
                    "step_done",
                    step_id=step.id,
                    tool=step.tool,
                    status=step_result.status,
                    attempts=step_result.attempts,
                )
            )
            if error:
                return results, error, tokens_used, trace
            completed[step.id] = step_result.payload
            tokens_used += cost
            if step_result.status == "completed":
                self.steps.save(key, step_result)
        return results, None, tokens_used, trace

    def _invoke_with_retries(
        self,
        packet: WorkPacket,
        step: PlanStep,
        args: dict[str, Any],
        rng: Any,
        *,
        dry_run: bool,
    ) -> tuple[StepResult, Optional[str]]:
        last_code = "tool_failed"
        last_transient = False
        attempts = 0
        for attempt in range(1, self.policy.max_attempts + 1):
            attempts = attempt
            self.logger.log(
                "step_start",
                packet_id=packet.packet_id,
                step_id=step.id,
                tool=step.tool,
                attempt=attempt,
            )
            try:
                payload = self.workspace.invoke(
                    packet.packet_id,
                    step.tool,
                    args,
                    dry_run=dry_run,
                    clearance=packet.workspace,
                )
            except ToolError as exc:
                last_code = exc.code
                last_transient = exc.transient
                # Only infrastructure faults count toward the breaker; business
                # outcomes such as slot_taken or not_found say nothing about tool health.
                if exc.transient:
                    self.breaker.record_failure(step.tool, self.clock.now_ms())
                retryable = is_retryable(exc.code, transient=exc.transient, policy=self.policy)
                if retryable and attempt < self.policy.max_attempts and self.breaker.allow(step.tool, self.clock.now_ms()):
                    delay = backoff_ms(self.policy, attempt, rng)
                    self.logger.log(
                        "step_retry",
                        packet_id=packet.packet_id,
                        step_id=step.id,
                        delay_ms=delay,
                        code=exc.code,
                    )
                    self.sleeper(delay)
                    continue
                return (
                    StepResult(
                        step_id=step.id,
                        tool=step.tool,
                        status="failed",
                        payload={"reason": last_code},
                        attempts=attempts,
                        dry_run=dry_run,
                    ),
                    last_code,
                )
            self.breaker.record_success(step.tool)
            return (
                StepResult(
                    step_id=step.id,
                    tool=step.tool,
                    status="completed",
                    payload=dict(payload),
                    attempts=attempts,
                    dry_run=dry_run,
                ),
                None,
            )
        return (
            StepResult(
                step_id=step.id,
                tool=step.tool,
                status="failed",
                payload={"reason": last_code, "transient": last_transient},
                attempts=attempts,
                dry_run=dry_run,
            ),
            last_code,
        )

    def _event(self, event: str, **fields: Any) -> TraceEvent:
        return TraceEvent(at_ms=self.clock.now_ms(), event=event, fields=dict(fields))

    def _result(
        self,
        packet: WorkPacket,
        *,
        run_id: str,
        status: str,
        attempts: int,
        trace: list[TraceEvent],
        dry_run: bool,
        plan: Optional[ToolPlan] = None,
        approval: Optional[str] = None,
        step_results: Optional[list[StepResult]] = None,
        error_code: Optional[str] = None,
        tokens_planned: int = 0,
        tokens_used: int = 0,
        steps_planned: int = 0,
    ) -> RunResult:
        result = RunResult(
            packet_id=packet.packet_id,
            status=status,
            contract_version=CONTRACT_VERSION,
            input_hash=packet.input_hash(),
            run_id=run_id,
            attempts=attempts,
            plan=plan,
            approval=approval,
            step_results=list(step_results or []),
            error_code=error_code,
            tokens_planned=tokens_planned,
            tokens_used=tokens_used,
            token_budget=packet.token_budget,
            steps_planned=steps_planned,
            trace=trace,
            cached=False,
            dry_run=dry_run,
        )
        self.logger.log("run_end", packet_id=packet.packet_id, status=status, attempts=attempts)
        result.trace.append(self._event("run_end", status=status, attempts=attempts))
        return result

    def _persist(
        self,
        packet: WorkPacket,
        result: RunResult,
        *,
        dry_run: bool,
        approve: bool,
    ) -> RunResult:
        if result.status == RunStatus.FAILED:
            self.logger.log("not_cached", packet_id=packet.packet_id, status=result.status)
            return result
        self.store.save(packet, result, dry_run=dry_run, approve=approve)
        return result
