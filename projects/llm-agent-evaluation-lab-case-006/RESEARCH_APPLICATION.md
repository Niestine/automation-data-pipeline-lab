# Research application

Case 006 uses ten public sources. Each technique below is in the named module and is locked by the named tests. Published savings percentages and published AIQ tables are citations. The pass line is this fixture's own comparison.

## Cascade routing by quality minus cost

Jasper Dekoninck, Maximilian Baader, and Martin Vechev, [A Unified Approach to Routing and Cascading for LLMs](https://arxiv.org/abs/2410.10347).

`routing.py` scores a supermodel with `tau = estimated_quality - lambda * estimated_cost`. Quality of a set is the max member quality. Cost of a set is the sum of member costs. The empty set is excluded. After a call, only sets that contain every model already used remain, and the next call is the cheapest unused member. A strictly negative marginal tau drops that model and every supermodel that contains the set. On a tie, probability `gamma` draws the cheapest tied set and the remainder draws the costliest. `gamma` of 0 returns the costliest tie and does not draw.

The product policy is this rule. A cheap-to-expensive ladder and a one-shot tau pick are baselines. A failed call sets that model's quality to 0 so another credential can still win tau.

Tests: `test_dekoninck_two_model_example`, `test_published_two_model_tau`, `test_gamma_tie_frequency`, `test_negative_marginal_gain_prunes_supersets`, `test_dekoninck_router_starts_on_the_expensive_model`, `test_stop_rule_threshold`.

## Three-part price, answer-agreement pruning, and a threshold grid

Lingjiao Chen, Matei Zaharia, and James Zou, [FrugalGPT: How to Use Large Language Models While Reducing Cost and Improving Performance](https://arxiv.org/abs/2305.05176).

`cost.py` charges `output_tokens * output_price + input_tokens * input_price + per_request`. The pre-call estimate uses `expected_output_tokens` from config, so price rank is recomputed per query. A card that is cheaper on a short completion can be the expensive card on a long one.

`fit.py` drops a more expensive model whose answer vector equals a cheaper kept model on the validation split, then searches a threshold grid and keeps the best accuracy whose mean cascade cost is inside the budget. The shipped validation split keeps `skiff` and `barque` and drops `yawl`. Both functions are unit-tested utilities. The shipped benchmark does not feed them back: it routes over all three models, and its threshold and lambdas are hand-set in `eval_fixture.json`. Fitting them on a same-distribution split is the step FrugalGPT requires, and this lab does not do it end to end.

Tests: `test_three_part_identity`, `test_estimate_uses_expected_output`, `test_long_output_reorders_price_cards`, `test_agreeing_models_are_pruned`, `test_threshold_grid_respects_budget`.

## Non-decreasing AIQ, Zero, Oracle, and judge noise

Qitian Jason Hu, Jacob Bieker, Xiuyu Li, Nan Jiang, Benjamin Keigwin, Gaurav Ranganath, Kurt Keutzer, and Shriyash Kaustubh Upadhyay, [RouterBench: A Benchmark for Multi-LLM Routing System](https://arxiv.org/abs/2403.12031).

`evaluate.py` builds the non-decreasing convex hull of `(cost, quality)` points, drops a point that costs more without a strictly higher quality, and drops a midpoint that sits on or below the chord. If the hull starts after `c_min`, the curve inserts `(c_min, 0)`. If it ends before `c_max`, it appends `(c_max, last quality)`. AIQ is the trapezoid area divided by `c_max - c_min`. A mix of two policies is their convex combination. Zero is the AIQ of the single-model points. The Oracle, used only in evaluation, is the cheapest model with gold at least 1 on each query. No policy quality in the scan is allowed above that ceiling.

The external scorer is a fixture table. With probability epsilon it is replaced by `1 - g`. Epsilon 0 does not draw. The scan uses `{0, 0.1, 0.2, 0.4}`. At 0 the product AIQ must beat Zero. At 0.4 the requirement is the budget, the attempt cap, the depth cap, and quality at or below the Oracle. `crossover_epsilon` is the smallest scanned epsilon where product AIQ is no longer above Zero. The budget check in the scan compares each run's priced (unclamped) spend with that run's budget and fails on any clamped charge. The Oracle is costed with the full price card on each query's input length.

On the shipped eval fixture the ex-ante quality estimate is the gold table. The epsilon-0 win over Zero is therefore a check of the routing and AIQ mechanics with a perfect ex-ante estimator, not evidence that a realistic estimator would win. Dekoninck's noise grid is the reason the next paragraph exists.

A second axis corrupts ex-ante quality, which moves the one-shot pick, or corrupts the post-hoc score, which can stop the threshold ladder early. The cascade may open on the expensive model when tau says so.

Tests: `test_aiq_of_the_unit_segment_is_one_half`, `test_extra_spend_without_quality_leaves_the_hull`, `test_mix_expectation_is_the_midpoint`, `test_oracle_on_the_three_model_fixture`, `test_oracle_and_benchmark_price_token_cards`, `test_product_beats_zero_and_respects_caps`, `test_noise_axes`.

## Vendor code before HTTP status

OpenAI, [API error codes](https://developers.openai.com/api/docs/guides/error-codes). Anthropic, [Claude API errors](https://platform.claude.com/docs/en/api/errors).

`classify.py` reads `error.code` and `error.type` before the HTTP status. `detail` and `message` are not lookup keys. OpenAI billing codes `credit_balance_exhausted`, `organization_spend_limit_exceeded`, `project_spend_limit_exceeded`, and `organization_usage_limit_exceeded` are terminal for that credential even when the type is `insufficient_quota`. The router problem status for that class is 402. `slow_down` and other rate-limit 429s use throttling backoff. `server_is_overloaded` / `service_unavailable_error` stay on 503. `service_tier` is a request defect. `RateLimitError` maps to throttling 429. `InternalServerError` maps to transient 500. Timeouts, including 504 and `timeout_error`, are not retried. The pre-call estimate is reserved when a timeout, including a bare HTTP 504, reports no usage. Connection failures and 511 are a network class: retried, and never cached.

Claude `billing_error` on 402 is terminal. A 400 carrying the lab `spend_limit` flag is terminal and labeled `spend_limit_400`. Other 400s, 413 `request_too_large`, and 431 are request defects: no retry and no same-body failover. 409 is not replayed inside the same `route()` call, and a later route for the same body is refused without a provider call until it sets `conflict_resolved`. A 429 with an integer Retry-After uses throttling backoff. A Claude 429 with no Retry-After is allowed one bounded backoff; a second consecutive bare 429 on the same credential becomes terminal billing (`first_bare_429`, `second_bare_429`). A success in between resets the count. That escalation is a labeled lab rule and is not applied to OpenAI. 529 `overloaded_error` is capacity: throttling retry, failover allowed. The fake provider forces `max_retries` to 0 so the router owns the only retry layer. An SSE error after HTTP 200 is not a successful completion and is not cached.

A rate-limit decision uses block `hold`, which lasts for the current route and is cleared on the next `route()`. Terminal billing and 401/403 use a persistent credential block. A 404 blocks that model id. Request defects and conflicts use block `all`.

Tests: `test_openai_billing_code_beats_insufficient_quota_type`, `test_detail_prose_is_not_the_lookup_key`, `test_slow_down_is_rate_limit_and_503_is_not`, `test_openai_500_is_transient`, `test_openai_bare_429_does_not_escalate`, `test_claude_spend_limit_and_billing`, `test_claude_bare_429_escalates_on_the_second_hit`, `test_claude_413_529_409_504`, `test_billing_does_not_failover_the_same_key`, `test_billing_can_use_a_different_credential`, `test_success_cache_and_rejected_statuses`, `test_claude_bare_429_second_hit_is_terminal`, `test_sse_after_200_is_not_success_or_cached`, `test_timeout_reserves_the_estimate`, `test_claude_504_without_usage_reserves_the_estimate`, `test_claude_bare_429_chain_resets_after_success`, `test_openai_sdk_exception_types_are_both_handled`, `test_completed_call_charges_the_formula`, `test_validation_and_fault_shape_stay_local`.

## Retry token bucket, separate from dollars

Amazon Web Services, [AWS SDKs and Tools: Retry behavior](https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html).

`bucket.py` keeps a token bucket of capacity 500. A transient retry costs 14. A throttling retry costs 5. A first-try HTTP success restores 1, capped at 500. A successful retry restores only that retry's own cost, so fail-fail-success on a transient fault ends at 486. An empty bucket fails the retry with no sleep. The first attempt is never delayed. There is no adaptive mode. Full jitter is `U(0, 1) * min(20000 ms, base * 2^retry_index)` with retry index starting at 0, base 50 ms for transient faults and 1000 ms for throttling. These constants are the SDK standard-mode policy coded in the lab. They are not an OpenAI or Claude contract. The default attempt cap is 3, including the first try. A failover is a new cascade step. A retry has to fit the remaining dollars before it pays retry tokens or sleeps.

Tests: `test_retry_that_cannot_fit_the_budget_costs_no_tokens_or_sleep`, `test_failed_retry_then_successful_retry_nets_minus_14`, `test_first_try_success_stays_at_capacity`, `test_empty_bucket_cannot_pay`, `test_throttling_base_is_longer_than_transient`, `test_bucket_blocks_a_retry_without_sleep_while_dollars_remain`, `test_successful_retry_nets_minus_14`, `test_throttling_code_uses_the_long_base`, `test_expensive_successes_drain_dollars_and_leave_the_bucket_full`.

AWS fixture codes are classified in the same function, code before status: `RequestTimeout` and `RequestTimeoutException` on 400 retry as transient; `ValidationException` does not; `ThrottlingException`, `TooManyRequestsException`, `Throttling`, `SlowDown`, and `ProvisionedThroughputExceededException` use the throttling bucket. Test: `test_aws_code_beats_status`, `test_aws_timeout_retries_and_validation_does_not`.

## HTTP status, Retry-After, and problem documents

R. Fielding, M. Nottingham, and J. Reschke, [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110). M. Nottingham and R. Fielding, [RFC 6585: Additional HTTP Status Codes](https://www.rfc-editor.org/rfc/rfc6585). M. Nottingham, E. Wilde, and S. Dalal, [RFC 9457: Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc9457).

Integer Retry-After is a minimum wait and is not clamped to 20 seconds, so `Retry-After: 30` waits at least 30000 ms. A non-integer value, including an HTTP-date, is `absent_or_unparsed` and uses jitter only. The archived RFC 9110 windows used for this lab do not contain the HTTP-date ABNF, so the lab does not parse that form and does not invent an idempotent-method replay rule. A 409 ends the route and records `(model, prompt_version, canonical query)`. A later `route()` for that body returns a `conflict` problem without calling the provider unless the request sets `conflict_resolved`; then one call is made and the record is cleared. A non-ASCII digit string in Retry-After is also left unparsed. 429 remains the rate-limit status after the vendor code has been read. 511 is the lab's network problem status. An unrecognized 499 with no vendor body is a request defect and is not retried.

`problems.py` emits one `application/problem+json` document per failure. `type` is an absolute URI under `https://portfolio.example/problems/` and is the classification key. `status` equals the router status. `title` is stable per slug. `detail` is human text and is not parsed. `instance` is `https://portfolio.example/instances/tide-NNNN`. Extensions are provider, model, failure class, retryable, failover, retry-after, budget remaining, and request id. Unknown extensions are ignored by `core_view`. A missing type reads as `about:blank`. Success is HTTP 200 `application/json` with the slip. Detail is `{model_id} {vendor code} classified as {failure_class}` and does not copy the provider message. A route where no model returns a valid slip ends with `contract-invalid` (422).

Tests: `test_integer_retry_after_only`, `test_non_ascii_digit_retry_after_is_unparsed`, `test_problem_detail_names_the_vendor_code_not_the_message`, `test_all_invalid_slips_return_contract_invalid`, `test_retry_after_is_a_minimum_and_is_not_clamped`, `test_retry_after_thirty_seconds_is_not_clamped`, `test_rfc9110_archive_gap_non_integer_retry_after_uses_jitter`, `test_conflict_is_not_replayed_immediately`, `test_unrecognized_499_is_not_retried`, `test_problem_detail_does_not_change_class`, `test_cheapest_over_budget_calls_nobody`, `test_network_511_is_not_cached`.

## Per-caller spend caps

OWASP GenAI Security Project, [LLM10:2025 Unbounded Consumption](https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/).

Before any provider call, an input over the token cap or the byte cap returns `input-too-large` and calls nobody. If the cheapest eligible estimate exceeds the caller's remaining dollars, the route returns `budget-exhausted` and calls nobody. A later stage whose estimate no longer fits is not called. Each caller has its own dollar balance, request quota, and token quota. A spike where the priced cost exceeds the dollars actually taken is logged as `consumption_spike` and the charge is clamped so spend stays inside the balance. An anomaly is recorded when `requests_started` exceeds `anomaly_rate`; the call can still proceed while it is under quota. Cascade depth and total HTTP completions are capped. Logprobs are not returned. The exact-match cache key is `(model_id, prompt_version, canonical query)` with collapsed whitespace. Only a successful above-threshold slip is stored. Cache hits charge 0. Statuses 428, 429, 431, and 511, any other non-2xx, and an SSE error after 200 are not stored.

Tests: `test_oversized_input_calls_nobody`, `test_cheapest_over_budget_calls_nobody`, `test_second_stage_over_budget_is_not_called`, `test_caller_budgets_are_independent`, `test_quota_and_anomaly`, `test_consumption_spike_is_logged`, `test_attempt_caps`, `test_depth_cap`, `test_success_cache_and_rejected_statuses`, `test_cache_hit_is_per_model`, `test_invalid_slip_is_not_cached`.
