"""Budgeted cascade router over a scripted provider.

Dollar spend and the retry token bucket are separate ledgers. The provider
client is constructed with max_retries 0 so this router is the only retry layer.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from tide_route.bucket import RetryBucket, retry_wait_ms
from tide_route.cache import UNCACHEABLE_STATUSES, CacheEntry, ExactCache
from tide_route.classify import Decision, classify
from tide_route.clock import FakeClock
from tide_route.cost import PriceCard, completion_cost, estimate_cost
from tide_route.problems import PROBLEM_CONTENT_TYPE, make_problem
from tide_route.provider import AttemptResult, FakeProvider, parse_slip
from tide_route.routing import next_model, ordered_by_cost, select_one
from tide_route.scorer import Scorer

TIMEOUT_ERROR_TYPES = frozenset({"timeout_error", "APITimeoutError"})


@dataclass
class ModelSpec:
    model_id: str
    provider: str
    credential_id: str
    price: PriceCard
    expected_output_tokens: int
    ex_ante: dict[str, float] = field(default_factory=dict)
    default_quality: float = 0.0

    def quality(self, query_id: str) -> float:
        return float(self.ex_ante.get(query_id, self.default_quality))


@dataclass
class CallerAccount:
    caller_id: str
    dollars: float
    request_quota: int = 100
    token_quota: int = 1_000_000
    anomaly_rate: int = 100
    requests_started: int = 0
    tokens_used: int = 0
    dollars_spent: float = 0.0


@dataclass
class RouterConfig:
    lambda_: float = 0.5
    gamma: float = 0.0
    thresholds: tuple[float, ...] = (0.5,)
    max_depth: int = 3
    max_attempts: int = 3
    max_total_attempts: int = 12
    max_input_tokens: int = 4096
    max_input_bytes: int = 32768
    attempt_timeout_s: float = 30.0
    anomaly_spend: float = 1_000_000.0


@dataclass
class RouteRequest:
    caller_id: str
    query_id: str
    query_text: str
    input_tokens: int
    input_bytes: int | None = None
    prompt_version: str = "slip-v1"
    conflict_resolved: bool = False
    policy: str = "cascade_routing"
    lambda_: float | None = None
    thresholds: tuple[float, ...] | None = None


@dataclass
class RouteOutcome:
    ok: bool
    http_status: int
    content_type: str
    caller_id: str
    query_id: str
    model_id: str | None
    slip: dict | None
    text: str | None
    charged: float
    priced: float
    problem: dict | None
    models_called: list[str]
    http_calls: int
    stop_reason: str
    trace: list[dict]
    from_cache: bool
    score: float | None


@dataclass
class _Invoke:
    http_ok: bool
    http_status: int
    sse_error: bool
    vendor_code: str
    slip: dict | None
    text: str
    score: float
    priced: float
    charged: float
    routing_cost: float
    fatal: bool
    decision: Decision | None
    from_cache: bool
    http_calls: int
    request_id: str
    model_id: str


class Router:
    def __init__(
        self,
        models: list[ModelSpec],
        accounts: dict[str, CallerAccount],
        provider: FakeProvider,
        *,
        scorer: Scorer | None = None,
        clock: FakeClock | None = None,
        rng: random.Random | None = None,
        bucket: RetryBucket | None = None,
        cache: ExactCache | None = None,
        config: RouterConfig | None = None,
    ) -> None:
        if provider.max_retries != 0:
            raise RuntimeError("provider max_retries must be 0")
        self.models = list(models)
        self.accounts = accounts
        self.provider = provider
        self.scorer = scorer or Scorer()
        self.clock = clock or FakeClock()
        self.rng = rng or random.Random(0)
        self.bucket = bucket or RetryBucket()
        self.cache = cache or ExactCache()
        self.config = config or RouterConfig()
        self.blocked_credentials: set[str] = set()
        self.blocked_models: set[str] = set()
        self._held: set[str] = set()
        self.bare_429: set[str] = set()
        # (model, prompt_version, canonical query) that received a 409. The same
        # body is not resent until a later request says the conflict is resolved.
        self.conflicts: set[tuple[str, str, str]] = set()
        self.anomalies: list[dict] = []
        self._request_no = 0
        self._route_attempts = 0

    def route(self, request: RouteRequest) -> RouteOutcome:
        self._request_no += 1
        self._route_attempts = 0
        self._held.clear()
        instance = f"https://portfolio.example/instances/tide-{self._request_no:04d}"
        trace: list[dict] = [
            {
                "event": "start",
                "conflict_resolved": request.conflict_resolved,
                "policy": request.policy,
            }
        ]
        account = self.accounts[request.caller_id]
        account.requests_started += 1
        if account.requests_started > account.anomaly_rate:
            self._anomaly(trace, account, "caller_rate_exceeded", request)
        byte_len = (
            request.input_bytes
            if request.input_bytes is not None
            else len(request.query_text.encode("utf-8"))
        )
        if (
            request.input_tokens > self.config.max_input_tokens
            or byte_len > self.config.max_input_bytes
        ):
            return self._finish_problem(
                request,
                account,
                trace,
                slug="input-too-large",
                detail="Input exceeds the configured token or byte cap",
                instance=instance,
                failure_class="input_too_large",
                stop_reason="input_cap",
            )
        if account.requests_started > account.request_quota:
            return self._finish_problem(
                request,
                account,
                trace,
                slug="caller-quota",
                detail="Caller request quota is exhausted",
                instance=instance,
                failure_class="caller_quota",
                stop_reason="request_quota",
            )
        if account.tokens_used + request.input_tokens > account.token_quota:
            return self._finish_problem(
                request,
                account,
                trace,
                slug="caller-quota",
                detail="Caller token quota is exhausted",
                instance=instance,
                failure_class="caller_quota",
                stop_reason="token_quota",
            )

        lambda_ = self.config.lambda_ if request.lambda_ is None else request.lambda_
        thresholds = self.config.thresholds if request.thresholds is None else request.thresholds
        costs = {
            model.model_id: estimate_cost(
                model.price, request.input_tokens, model.expected_output_tokens
            )
            for model in self.models
        }
        eligible = [model for model in self.models if self._eligible(model.model_id)]
        if not eligible:
            return self._finish_problem(
                request,
                account,
                trace,
                slug="unavailable",
                detail="No eligible provider remains",
                instance=instance,
                failure_class="transient",
                stop_reason="none_eligible",
            )
        cheapest = min(costs[model.model_id] for model in eligible)
        if cheapest > account.dollars + 1e-12:
            return self._finish_problem(
                request,
                account,
                trace,
                slug="budget-exhausted",
                detail="Cheapest eligible estimate exceeds the remaining budget",
                instance=instance,
                failure_class="budget_exhausted",
                stop_reason="budget",
            )

        qualities = {model.model_id: model.quality(request.query_id) for model in self.models}
        called: list[str] = []
        best: _Invoke | None = None
        last: _Invoke | None = None
        charged = 0.0
        priced = 0.0
        policy = request.policy

        for step in range(self.config.max_depth):
            choice = self._choose(
                policy,
                step,
                called,
                qualities,
                costs,
                lambda_,
            )
            if choice is None:
                trace.append({"event": "stop", "reason": "no_remaining_model"})
                break
            estimate = costs[choice]
            if estimate > account.dollars + 1e-12:
                trace.append(
                    {
                        "event": "budget_stop",
                        "model": choice,
                        "estimate": estimate,
                        "budget_remaining": account.dollars,
                    }
                )
                break
            model = self._model(choice)
            invoked = self._invoke(model, request, account, trace)
            called.append(choice)
            charged += invoked.charged
            priced += invoked.priced
            if invoked.routing_cost > 0:
                costs[choice] = invoked.routing_cost
            last = invoked
            if invoked.slip is not None:
                qualities[choice] = invoked.score
                if best is None or invoked.score >= best.score:
                    best = invoked
                threshold = thresholds[min(step, len(thresholds) - 1)]
                if invoked.score >= threshold:
                    self._remember(request, invoked)
                    return self._success(
                        request,
                        invoked,
                        called,
                        charged,
                        priced,
                        trace,
                        "threshold" if policy != "one_shot" else "one_shot",
                    )
            else:
                qualities[choice] = 0.0
            if invoked.fatal:
                return self._from_invoke(
                    request, account, invoked, called, charged, priced, trace, instance
                )
            if policy == "one_shot":
                break

        if best is not None and best.slip is not None:
            reason = "one_shot" if policy == "one_shot" else "no_better_tau"
            return self._success(request, best, called, charged, priced, trace, reason)
        if last is not None and last.decision is not None:
            return self._from_invoke(
                request, account, last, called, charged, priced, trace, instance
            )
        if last is not None and last.http_ok and last.slip is None:
            return self._finish_problem(
                request,
                account,
                trace,
                slug="contract-invalid",
                detail="No provider returned a slip that matches the closed contract",
                instance=instance,
                failure_class="contract_invalid",
                stop_reason="contract_invalid",
                charged=charged,
                priced=priced,
                models_called=called,
                model_id=last.model_id,
                provider=self._model(last.model_id).provider,
            )
        return self._finish_problem(
            request,
            account,
            trace,
            slug="budget-exhausted",
            detail="No accepted answer inside the remaining budget",
            instance=instance,
            failure_class="budget_exhausted",
            stop_reason="budget",
            charged=charged,
            priced=priced,
            models_called=called,
        )

    def _choose(self, policy, step, called, qualities, costs, lambda_) -> str | None:
        called_set = set(called)
        visible = [
            model.model_id
            for model in self.models
            if model.model_id in called_set or self._eligible(model.model_id)
        ]
        unused = [model_id for model_id in visible if model_id not in called_set]
        if policy == "one_shot":
            if step > 0:
                return None
            return select_one(unused, qualities, costs, lambda_, self.config.gamma, self.rng)
        if policy == "threshold_cascade":
            ladder = ordered_by_cost(unused, costs)
            return ladder[0] if ladder else None
        return next_model(
            visible,
            qualities,
            costs,
            called_set,
            lambda_,
            self.config.gamma,
            self.rng,
        )

    def _eligible(self, model_id: str) -> bool:
        model = self._model(model_id)
        if model_id in self.blocked_models:
            return False
        if model.credential_id in self.blocked_credentials or model.credential_id in self._held:
            return False
        return True

    def _model(self, model_id: str) -> ModelSpec:
        for model in self.models:
            if model.model_id == model_id:
                return model
        raise KeyError(model_id)

    def _invoke(self, model, request, account, trace) -> _Invoke:
        conflict_key = self.cache.key(model.model_id, request.prompt_version, request.query_text)
        if conflict_key in self.conflicts:
            if not request.conflict_resolved:
                trace.append({"event": "conflict_pending", "model": model.model_id})
                return _failed(
                    model,
                    _conflict_decision(),
                    status=409,
                    vendor_code="conflict_pending",
                    priced=0.0,
                    charged=0.0,
                    http_calls=0,
                    request_id="",
                )
            self.conflicts.discard(conflict_key)
            trace.append({"event": "conflict_cleared", "model": model.model_id})

        cached = self.cache.get(model.model_id, request.prompt_version, request.query_text)
        if cached is not None:
            slip = parse_slip(cached.text)
            score = self.scorer.score(request.query_id, model.model_id, cached.text) if slip else 0.0
            trace.append(
                {
                    "event": "cache_hit",
                    "model": model.model_id,
                    "provider": model.provider,
                    "request_id": cached.request_id,
                }
            )
            return _Invoke(
                http_ok=True,
                http_status=200,
                sse_error=False,
                vendor_code="",
                slip=slip,
                text=cached.text,
                score=score,
                priced=0.0,
                charged=0.0,
                routing_cost=cached.priced,
                fatal=False,
                decision=None,
                from_cache=True,
                http_calls=0,
                request_id=cached.request_id,
                model_id=model.model_id,
            )

        retry_index = 0
        paid = 0
        http_calls = 0
        last_decision: Decision | None = None
        last_attempt: AttemptResult | None = None
        priced_sum = 0.0
        charged_sum = 0.0

        def failed(decision: Decision | None, *, apply_blocks: bool = True) -> _Invoke:
            if apply_blocks:
                self._apply_blocks(model, decision)
            return _failed(
                model,
                decision,
                status=last_attempt.status if last_attempt else 0,
                vendor_code=_vendor_code(last_attempt),
                priced=priced_sum,
                charged=charged_sum,
                http_calls=http_calls,
                request_id=last_attempt.request_id if last_attempt else "",
            )

        estimate = estimate_cost(model.price, request.input_tokens, model.expected_output_tokens)
        while True:
            if self._route_attempts >= self.config.max_total_attempts:
                trace.append({"event": "attempt_cap", "model": model.model_id})
                return failed(last_decision or _cap_decision())
            if estimate > account.dollars + 1e-12:
                trace.append(
                    {
                        "event": "budget_stop",
                        "model": model.model_id,
                        "estimate": estimate,
                        "budget_remaining": account.dollars,
                    }
                )
                return failed(last_decision, apply_blocks=False)

            self._route_attempts += 1
            http_calls += 1
            attempt = self.provider.complete(
                model.model_id, request.query_text, self.config.attempt_timeout_s
            )
            last_attempt = attempt
            reserve = _timeout_without_usage(attempt)
            priced = _priced(model, request, attempt, reserve)
            charged = self._charge(account, priced, trace, request, model)
            priced_sum += priced
            charged_sum += charged
            account.tokens_used += _token_count(request, attempt, model, reserve)
            if priced >= self.config.anomaly_spend:
                self._anomaly(trace, account, "consumption_spike", request, model.model_id, priced)

            if attempt.ok:
                if retry_index == 0:
                    self.bucket.on_first_try_success()
                else:
                    self.bucket.on_retry_success(paid)
                # A success breaks the "consecutive bare 429" chain for this credential.
                self.bare_429.discard(model.credential_id)
                slip = parse_slip(attempt.text)
                score = (
                    self.scorer.score(request.query_id, model.model_id, attempt.text)
                    if slip is not None
                    else 0.0
                )
                trace.append(
                    {
                        "event": "attempt",
                        "model": model.model_id,
                        "provider": model.provider,
                        "status": attempt.status,
                        "failure_class": "" if slip is not None else "contract_invalid",
                        "priced": priced,
                        "charged": charged,
                        "bucket": self.bucket.balance,
                        "budget_remaining": account.dollars,
                        "request_id": attempt.request_id,
                    }
                )
                return _Invoke(
                    http_ok=True,
                    http_status=attempt.status,
                    sse_error=attempt.sse_error,
                    vendor_code="",
                    slip=slip,
                    text=attempt.text,
                    score=score,
                    priced=priced_sum,
                    charged=charged_sum,
                    routing_cost=priced_sum,
                    fatal=False,
                    decision=None,
                    from_cache=False,
                    http_calls=http_calls,
                    request_id=attempt.request_id,
                    model_id=model.model_id,
                )

            decision = self._decide(model, attempt)
            last_decision = decision
            trace.append(
                {
                    "event": "attempt",
                    "model": model.model_id,
                    "provider": model.provider,
                    "status": attempt.status,
                    "failure_class": decision.failure_class,
                    "retryable": decision.retryable,
                    "priced": priced,
                    "charged": charged,
                    "reserved_estimate": reserve,
                    "bucket": self.bucket.balance,
                    "budget_remaining": account.dollars,
                    "request_id": attempt.request_id,
                    "retry_after_parse": decision.retry_after_parse,
                    "lab_rule": decision.lab_rule,
                }
            )
            if decision.lab_rule == "first_bare_429":
                self.bare_429.add(model.credential_id)
            elif model.provider == "anthropic" and decision.lab_rule != "second_bare_429":
                self.bare_429.discard(model.credential_id)
            if decision.failure_class == "conflict":
                self.conflicts.add(conflict_key)

            if http_calls >= self.config.max_attempts or not decision.retryable:
                return failed(decision)
            if not decision.retry_kind or not self.bucket.can_pay(decision.retry_kind):
                trace.append(
                    {
                        "event": "retry_blocked",
                        "model": model.model_id,
                        "bucket": self.bucket.balance,
                        "failure_class": decision.failure_class,
                    }
                )
                return failed(decision)
            if self._route_attempts >= self.config.max_total_attempts:
                trace.append({"event": "attempt_cap", "model": model.model_id})
                return failed(decision)
            if estimate > account.dollars + 1e-12:
                # A retry must fit the dollar budget too. Check before paying
                # retry tokens or sleeping, so a doomed retry costs nothing.
                trace.append(
                    {
                        "event": "budget_stop",
                        "model": model.model_id,
                        "estimate": estimate,
                        "budget_remaining": account.dollars,
                    }
                )
                return failed(decision)
            paid = self.bucket.charge(decision.retry_kind)
            delay_ms, parse_flag = retry_wait_ms(
                self.rng,
                retry_index,
                decision.retry_kind,
                decision.retry_after_seconds,
                decision.retry_after_parse,
            )
            self.clock.sleep(delay_ms)
            trace.append(
                {
                    "event": "retry_sleep",
                    "model": model.model_id,
                    "sleep_ms": delay_ms,
                    "retry_index": retry_index,
                    "retry_kind": decision.retry_kind,
                    "retry_after_parse": parse_flag,
                    "bucket": self.bucket.balance,
                }
            )
            retry_index += 1

    def _decide(self, model: ModelSpec, attempt: AttemptResult) -> Decision:
        seen = model.credential_id in self.bare_429
        return classify(
            vendor=model.provider,
            status=attempt.status,
            error_code=attempt.error_code,
            error_type=attempt.error_type,
            headers=attempt.headers,
            timeout=attempt.timeout,
            network=attempt.network,
            sse_error=attempt.sse_error,
            spend_limit=attempt.spend_limit,
            bare_429_seen=seen,
            message=attempt.error_message,
        )

    def _apply_blocks(self, model: ModelSpec, decision: Decision | None) -> None:
        if decision is None:
            return
        if decision.blocks == "credential":
            self.blocked_credentials.add(model.credential_id)
        elif decision.blocks == "hold":
            self._held.add(model.credential_id)
        elif decision.blocks == "model":
            self.blocked_models.add(model.model_id)

    def _charge(self, account, amount, trace, request, model) -> float:
        if amount <= 0:
            return 0.0
        taken = amount if amount <= account.dollars + 1e-12 else account.dollars
        account.dollars -= taken
        account.dollars_spent += taken
        if amount > taken + 1e-9:
            self._anomaly(trace, account, "consumption_spike", request, model.model_id, amount)
        return taken

    def _anomaly(self, trace, account, kind, request, model_id=None, priced=None) -> None:
        event = {
            "event": "anomaly",
            "kind": kind,
            "caller": account.caller_id,
            "query_id": request.query_id,
            "model": model_id,
            "priced": priced,
            "requests_started": account.requests_started,
        }
        trace.append(event)
        self.anomalies.append(event)

    def _remember(self, request: RouteRequest, invoked: _Invoke) -> None:
        if invoked.from_cache or invoked.slip is None:
            return
        if not _cache_allowed(invoked.http_status, invoked.sse_error):
            return
        self.cache.store(
            invoked.model_id,
            request.prompt_version,
            request.query_text,
            CacheEntry(invoked.text, invoked.routing_cost, invoked.request_id),
        )

    def _success(self, request, invoked, called, charged, priced, trace, reason) -> RouteOutcome:
        trace.append({"event": "stop", "reason": reason, "model": invoked.model_id})
        return RouteOutcome(
            ok=True,
            http_status=200,
            content_type="application/json",
            caller_id=request.caller_id,
            query_id=request.query_id,
            model_id=invoked.model_id,
            slip=invoked.slip,
            text=invoked.text,
            charged=charged,
            priced=priced,
            problem=None,
            models_called=list(called),
            http_calls=self._route_attempts,
            stop_reason=reason,
            trace=trace,
            from_cache=invoked.from_cache,
            score=invoked.score,
        )

    def _from_invoke(self, request, account, invoked, called, charged, priced, trace, instance):
        decision = invoked.decision
        slug = decision.problem_slug if decision else "unavailable"
        status = decision.http_status if decision else 503
        failure_class = decision.failure_class if decision else "transient"
        detail = _safe_detail(invoked.model_id, invoked.vendor_code, failure_class)
        retryable = bool(decision.retryable) if decision else False
        failover = bool(decision.failover_allowed) if decision else False
        retry_after_ms = None
        if decision and decision.retry_after_parse == "integer" and decision.retry_after_seconds is not None:
            retry_after_ms = decision.retry_after_seconds * 1000
        problem = make_problem(
            slug,
            detail=detail,
            instance=instance,
            provider=self._model(invoked.model_id).provider,
            model=invoked.model_id,
            failure_class=failure_class,
            retryable=retryable,
            failover_allowed=failover,
            retry_after_ms=retry_after_ms,
            budget_remaining=account.dollars,
            request_id=invoked.request_id or None,
            status=status,
        )
        return RouteOutcome(
            ok=False,
            http_status=problem["status"],
            content_type=PROBLEM_CONTENT_TYPE,
            caller_id=request.caller_id,
            query_id=request.query_id,
            model_id=invoked.model_id,
            slip=None,
            text=None,
            charged=charged,
            priced=priced,
            problem=problem,
            models_called=list(called),
            http_calls=self._route_attempts,
            stop_reason=failure_class,
            trace=trace,
            from_cache=False,
            score=None,
        )

    def _finish_problem(
        self,
        request,
        account,
        trace,
        *,
        slug,
        detail,
        instance,
        failure_class,
        stop_reason,
        charged=0.0,
        priced=0.0,
        models_called=None,
        model_id=None,
        provider=None,
        status=None,
    ) -> RouteOutcome:
        problem = make_problem(
            slug,
            detail=detail,
            instance=instance,
            provider=provider,
            model=model_id,
            failure_class=failure_class,
            retryable=False,
            failover_allowed=False,
            budget_remaining=account.dollars,
            status=status,
        )
        return RouteOutcome(
            ok=False,
            http_status=problem["status"],
            content_type=PROBLEM_CONTENT_TYPE,
            caller_id=request.caller_id,
            query_id=request.query_id,
            model_id=model_id,
            slip=None,
            text=None,
            charged=charged,
            priced=priced,
            problem=problem,
            models_called=list(models_called or []),
            http_calls=self._route_attempts,
            stop_reason=stop_reason,
            trace=trace,
            from_cache=False,
            score=None,
        )


def _failed(
    model: ModelSpec,
    decision: Decision | None,
    *,
    status: int,
    vendor_code: str,
    priced: float,
    charged: float,
    http_calls: int,
    request_id: str,
) -> _Invoke:
    return _Invoke(
        http_ok=False,
        http_status=status,
        sse_error=False,
        vendor_code=vendor_code,
        slip=None,
        text="",
        score=0.0,
        priced=priced,
        charged=charged,
        routing_cost=priced,
        fatal=bool(decision and decision.blocks == "all"),
        decision=decision,
        from_cache=False,
        http_calls=http_calls,
        request_id=request_id,
        model_id=model.model_id,
    )


def _timeout_without_usage(attempt: AttemptResult) -> bool:
    """A timeout (exception, HTTP 504, or timeout type) that reported no token usage."""
    timed_out = (
        attempt.timeout
        or attempt.status == 504
        or (attempt.error_type or "") in TIMEOUT_ERROR_TYPES
    )
    return timed_out and attempt.input_tokens is None and attempt.output_tokens is None


def _priced(model: ModelSpec, request: RouteRequest, attempt: AttemptResult, reserve: bool) -> float:
    if reserve:
        return estimate_cost(model.price, request.input_tokens, model.expected_output_tokens)
    in_tokens = request.input_tokens if attempt.input_tokens is None else attempt.input_tokens
    out_tokens = 0 if attempt.output_tokens is None else attempt.output_tokens
    return completion_cost(model.price, in_tokens, out_tokens)


def _token_count(request, attempt, model, reserve: bool) -> int:
    if reserve:
        return request.input_tokens + model.expected_output_tokens
    in_tokens = request.input_tokens if attempt.input_tokens is None else attempt.input_tokens
    out_tokens = 0 if attempt.output_tokens is None else attempt.output_tokens
    return in_tokens + out_tokens


def _cache_allowed(status: int, sse_error: bool) -> bool:
    if sse_error or status in UNCACHEABLE_STATUSES:
        return False
    return 200 <= status < 300


def _vendor_code(attempt: AttemptResult | None) -> str:
    if attempt is None:
        return ""
    if attempt.timeout:
        return "timeout"
    if attempt.network:
        return "connection_error"
    if attempt.sse_error:
        return "sse_error_after_200"
    return attempt.error_code or attempt.error_type or f"http_{attempt.status}"


def _safe_detail(model_id: str, vendor_code: str, failure_class: str) -> str:
    """Identifiers only. The provider's message text is never copied."""
    return f"{model_id} {vendor_code or 'no_response'} classified as {failure_class}"


def _cap_decision() -> Decision:
    return Decision(
        failure_class="transient",
        retryable=False,
        failover_allowed=False,
        retry_kind="",
        problem_slug="unavailable",
        http_status=503,
        blocks="",
        retry_after_seconds=None,
        retry_after_parse="absent_or_unparsed",
    )


def _conflict_decision() -> Decision:
    return Decision(
        failure_class="conflict",
        retryable=False,
        failover_allowed=False,
        retry_kind="",
        problem_slug="conflict",
        http_status=409,
        blocks="all",
        retry_after_seconds=None,
        retry_after_parse="absent_or_unparsed",
    )
