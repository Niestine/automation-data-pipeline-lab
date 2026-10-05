"""Loop desk and re-queue desk for one bay-hold notice.

Both forms stamp a token, reset the attempt buffer, and log the same fields.
Defect flags are the historical builds the oracles reject.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .budget import SharedBudget
from .clock import VirtualClock
from .errors import (
    BudgetExhausted,
    OverloadedSignal,
    PermanentGateError,
    TransientGateError,
    make_error,
)
from .handlers import dispatch_error
from .inject import Gateway, Job, Step
from .ledger import Ledger
from .log import emit_attempt
from .policy import JitterSampler, RetryPolicy, apply_jitter
from .timer import RtoEstimator


@dataclass
class Defects:
    no_backoff: bool = False
    ignore_cap: bool = False
    accept_stale: bool = False
    concat_partial: bool = False
    retry_permanent: bool = False
    skip_transient: bool = False
    handler: str = "repair"
    requeue_drops_cap: bool = False
    requeue_drops_delay: bool = False


BUILDS: dict[str, Defects] = {
    "repaired": Defects(),
    "no-backoff": Defects(no_backoff=True),
    "ignore-cap": Defects(ignore_cap=True),
    "accept-stale": Defects(accept_stale=True),
    "concat-partial": Defects(concat_partial=True),
    "retry-permanent": Defects(retry_permanent=True),
    "skip-transient": Defects(skip_transient=True),
    "empty-handler": Defects(handler="empty"),
    "swallow-handler": Defects(handler="swallow"),
    "rewrite-handler": Defects(handler="rewrite"),
    "broad-abort": Defects(handler="broad"),
    "requeue-drops-cap": Defects(requeue_drops_cap=True),
    "requeue-drops-delay": Defects(requeue_drops_delay=True),
}


@dataclass
class Result:
    operation_id: str
    ok: bool
    body: str
    transmissions: int
    commits: int
    decisions: list[str]


@dataclass
class Attempt:
    job: Job
    attempt: int = 0
    token: str = ""
    buffer: str = ""
    last_error: BaseException | None = None


@dataclass
class Desk:
    policy: RetryPolicy
    gateway: Gateway
    timer: RtoEstimator
    clock: VirtualClock = field(default_factory=VirtualClock)
    ledger: Ledger = field(default_factory=Ledger)
    budget: SharedBudget | None = None
    defects: Defects = field(default_factory=Defects)
    sampler: object | None = None
    template: str = "bay {operation_id} attempt {attempt} decision {decision}"
    transmissions: int = 0
    decisions: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    result_body: str = ""
    ok: bool = False
    job: Job | None = None
    last_token: str = ""

    def __post_init__(self) -> None:
        if self.sampler is None:
            self.sampler = JitterSampler(self.policy.jitter, self.policy.seed, self.policy.spread)

    def run(self, job: Job) -> Result:
        """In-process loop. About the shape used by the plain-loop half of the study."""

        job.validate()
        self._begin(job)
        current = Attempt(job=job)
        while True:
            self._transmit(current)
            action = self._collect(current, requeue=False)
            if action in {"done", "fake-success"}:
                return self._result(current)
            if action == "raise":
                assert current.last_error is not None
                raise current.last_error
            if action == "retry":
                current = self._next(current)
                continue
            raise RuntimeError(f"unknown action {action!r}")

    def run_requeue(self, job: Job) -> Result:
        """Work list. A failed transient goes back on the deque with the same rules."""

        job.validate()
        self._begin(job)
        queue: deque[Attempt] = deque([Attempt(job=job)])
        while queue:
            current = queue.popleft()
            self._transmit(current)
            action = self._collect(current, requeue=True)
            if action in {"done", "fake-success"}:
                return self._result(current)
            if action == "raise":
                assert current.last_error is not None
                raise current.last_error
            if action == "retry":
                queue.append(self._next(current))
                continue
            raise RuntimeError(f"unknown action {action!r}")
        raise RuntimeError("requeue stopped without a result")

    def deliver(self, step: Step) -> bool:
        """A response that arrives after the notice already finished.

        It never takes an RTT sample. The repair commits only a token that
        matches the attempt that succeeded, and never after the desk gave up.
        """

        if self.job is None:
            raise RuntimeError("deliver before run")
        token = step.token or ""
        if self.defects.accept_stale:
            return self.ledger.commit_token(
                self.job.operation_id, token, step.body or self.result_body, self.job.charge
            )
        if not self.ok or not token or token != self.last_token:
            return False
        return self.ledger.commit(self.job.operation_id, token, self.result_body, self.job.charge)

    def _begin(self, job: Job) -> None:
        """Per-notice state. The ledger, timer, clock, and budget outlive one notice."""

        self.job = job
        self.ok = False
        self.result_body = ""
        self.last_token = ""
        self.transmissions = 0
        self.decisions = []
        self.events = []

    def _next(self, current: Attempt) -> Attempt:
        return Attempt(
            job=current.job,
            attempt=current.attempt + 1,
            buffer=current.buffer if self.defects.concat_partial else "",
        )

    def _transmit(self, current: Attempt) -> None:
        if not self.defects.concat_partial:
            current.buffer = ""
        current.token = f"{current.job.operation_id}.{current.attempt}"
        self.transmissions += 1
        self.gateway.note_send()

    def _collect(self, current: Attempt, requeue: bool) -> str:
        while True:
            step = self.gateway.receive(current.token)
            if step.kind in {"success", "late"}:
                if step.token != current.token:
                    self._note_stale(current, step)
                    continue
                return self._on_success(current, step)
            if step.kind == "overloaded":
                return self._on_overloaded(current)
            if step.kind == "timeout":
                error: BaseException = TransientGateError("armed rto elapsed")
            elif step.kind == "error":
                error = self._error_from(step)
            else:
                raise ValueError(f"unknown step kind {step.kind!r}")
            if step.partial:
                current.buffer += step.partial
            return self._on_error(current, error, requeue)

    def _error_from(self, step: Step) -> BaseException:
        if step.error is not None:
            return step.error
        if step.error_name:
            return make_error(step.error_name)
        return TransientGateError("gate error")

    def _on_success(self, current: Attempt, step: Step) -> str:
        self.timer.observe(step.rtt_s, True)
        if self.budget is not None:
            self.budget.observe(False)
            self.budget.note_status("OK")
            self.budget.tick()
        body = current.buffer + step.body if self.defects.concat_partial else step.body
        self.result_body = body
        self.ok = True
        self.last_token = current.token
        if self.defects.accept_stale:
            self.ledger.commit_token(current.job.operation_id, current.token, body, current.job.charge)
        else:
            self.ledger.commit(current.job.operation_id, current.token, body, current.job.charge)
        self._record(current, "stop", "", 0.0, self.timer.rto)
        return "done"

    def _note_stale(self, current: Attempt, step: Step) -> None:
        if self.defects.accept_stale:
            self.timer.observe(step.rtt_s, True)
            self.ledger.commit_token(
                current.job.operation_id,
                step.token or "",
                step.body,
                current.job.charge,
            )
        self.events.append(
            {
                "outstanding": current.token,
                "got": step.token,
                "commits": len(self.ledger.entries),
                "srtt": self.timer.srtt,
            }
        )

    def _on_overloaded(self, current: Attempt) -> str:
        error = OverloadedSignal("gate overloaded")
        current.last_error = error
        if self.budget is not None:
            self.budget.observe(True)
            self.budget.note_status("OVERLOADED")
            self.budget.tick()
        self._record(current, "budget_refuse", type(error).__name__, 0.0, self.timer.rto)
        return "raise"

    def _on_error(self, current: Attempt, error: BaseException, requeue: bool) -> str:
        current.last_error = error
        try:
            action = dispatch_error(self.defects.handler, error, self.policy, self.defects)
        except PermanentGateError:
            self._record(current, "not_retryable", type(error).__name__, 0.0, self.timer.rto)
            raise
        except BudgetExhausted:
            self._record(current, "budget_refuse", type(error).__name__, 0.0, self.timer.rto)
            raise
        except Exception:
            self._record(current, "stop", type(error).__name__, 0.0, self.timer.rto)
            raise
        if action == "fake-success":
            self.ok = True
            self.result_body = ""
            return "fake-success"
        if action == "overloaded":
            return self._on_overloaded(current)
        if action != "retry":
            raise RuntimeError(f"unexpected handler action {action!r}")
        if self.budget is not None:
            self.budget.observe(True)
            self.budget.note_status("OK")
            self.budget.tick()
        # The cap is checked first so a retry the cap forbids never spends shared budget.
        if not self._can_retry(current.attempt, requeue):
            self._record(current, "stop", type(error).__name__, 0.0, self.timer.rto)
            return "raise"
        if self.budget is not None and not self.budget.try_admit():
            current.last_error = BudgetExhausted(current.job.operation_id)
            self._record(current, "budget_refuse", type(error).__name__, 0.0, self.timer.rto)
            return "raise"
        self._pause(current, self._honor_delay(requeue), type(error).__name__)
        return "retry"

    def _can_retry(self, attempt: int, requeue: bool) -> bool:
        return attempt + 1 <= self._limit(requeue)

    def _limit(self, requeue: bool) -> int:
        # The extra slot exists so a broken cap is observable and then stops.
        extra = 0
        if self.defects.ignore_cap:
            extra = 1
        if requeue and self.defects.requeue_drops_cap:
            extra = 1
        return self.policy.max_retries + extra

    def _honor_delay(self, requeue: bool) -> bool:
        if self.defects.no_backoff:
            return False
        if requeue and self.defects.requeue_drops_delay:
            return False
        return True

    def _pause(self, current: Attempt, honor_delay: bool, error_type: str) -> None:
        armed = self.timer.rto
        if honor_delay:
            delay = apply_jitter(
                armed,
                self.policy.floor_s,
                self.policy.ceiling_s,
                self.sampler.factor(),  # type: ignore[union-attr]
            )
        else:
            delay = 0.0
        self._record(current, "retry", error_type, delay, armed)
        self.clock.sleep(delay)
        self.timer.on_timeout()

    def _record(
        self,
        current: Attempt,
        decision: str,
        error_type: str,
        delay_s: float,
        rto_s: float,
    ) -> None:
        self.decisions.append(decision)
        emit_attempt(
            self.template,
            current.job.operation_id,
            current.attempt,
            current.token,
            error_type,
            decision,
            delay_s,
            rto_s,
        )

    def _result(self, current: Attempt) -> Result:
        return Result(
            operation_id=current.job.operation_id,
            ok=self.ok,
            body=self.result_body,
            transmissions=self.transmissions,
            commits=len(self.ledger.entries),
            decisions=list(self.decisions),
        )
