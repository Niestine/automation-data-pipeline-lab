"""Router behavior: stop rule, failures, budgets, cache, and caps."""

from __future__ import annotations

import json
import unittest

from helpers import EXAMPLES, SeqRNG, make_router, model, request
from tide_route.cache import UNCACHEABLE_STATUSES
from tide_route.provider import ScriptStep, parse_slip
from tide_route.router import CallerAccount


def codes(router):
    return [call["model_id"] for call in router.provider.calls]


class RouterTests(unittest.TestCase):
    def test_stop_rule_threshold(self):
        models = [
            model("low", per_request=0.2, ex_ante={"Q": 0.9}, default_quality=0.9),
            model("high", per_request=2.0, ex_ante={"Q": 0.95}, default_quality=0.95),
        ]
        high = make_router(
            models,
            scores={("Q", "low"): 0.97, ("Q", "high"): 0.99},
            threshold=0.96,
            lambda_=0.1,
        )
        outcome = high.route(request())
        self.assertEqual(outcome.models_called, ["low"])
        self.assertEqual(codes(high), ["low"])
        self.assertEqual(outcome.stop_reason, "threshold")

        low = make_router(
            models,
            scores={("Q", "low"): 0.17, ("Q", "high"): 0.99},
            threshold=0.96,
            lambda_=0.1,
        )
        outcome = low.route(request())
        self.assertEqual(outcome.models_called, ["low", "high"])
        self.assertEqual(codes(low), ["low", "high"])

    def test_dekoninck_router_starts_on_the_expensive_model(self):
        models = [
            model("cheap", per_request=0.5, ex_ante={"Q": 0.5}),
            model("expensive", per_request=1.0, ex_ante={"Q": 0.8}),
        ]
        started = make_router(
            models,
            scores={("Q", "cheap"): 0.5, ("Q", "expensive"): 0.99},
            lambda_=0.1,
            threshold=0.5,
        )
        outcome = started.route(request())
        self.assertEqual(outcome.models_called, ["expensive"])

        revised = make_router(
            models,
            scores={("Q", "cheap"): 0.5, ("Q", "expensive"): 0.1},
            lambda_=1.0,
            threshold=0.96,
        )
        cheap_first = revised.route(request())
        self.assertEqual(cheap_first.models_called[0], "cheap")

        follow = make_router(
            models,
            scores={("Q", "cheap"): 0.99, ("Q", "expensive"): 0.1},
            lambda_=0.1,
            threshold=0.96,
        )
        outcome = follow.route(request())
        self.assertEqual(outcome.models_called, ["expensive", "cheap"])

    def test_depth_cap(self):
        models = [
            model("low", per_request=0.2, ex_ante={"Q": 0.9}),
            model("high", per_request=2.0, ex_ante={"Q": 0.95}),
        ]
        router = make_router(
            models,
            scores={("Q", "low"): 0.17, ("Q", "high"): 0.99},
            threshold=0.96,
            lambda_=0.1,
            max_depth=1,
        )
        outcome = router.route(request())
        self.assertEqual(outcome.models_called, ["low"])

    def test_completed_call_charges_the_formula(self):
        spec = model(
            "skiff",
            per_request=0.125,
            input_per_token=0.5,
            output_per_token=0.25,
            expected_output_tokens=4,
        )
        router = make_router(
            [spec],
            scripts={
                "skiff": [ScriptStep(input_tokens=2, output_tokens=4, request_id="req-1")]
            },
        )
        outcome = router.route(request(tokens=2))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.charged, 0.25 * 4 + 0.5 * 2 + 0.125)
        self.assertEqual(router.provider.max_retries, 0)
        self.assertEqual(router.provider.calls[0]["timeout_s"], 30.0)
        self.assertFalse(hasattr(outcome, "logprobs"))

    def test_second_stage_over_budget_is_not_called(self):
        models = [
            model("first", per_request=1.0, ex_ante={"Q": 0.99}),
            model("second", per_request=1.0, ex_ante={"Q": 0.5}),
        ]
        router = make_router(
            models,
            dollars=1.0,
            scores={("Q", "first"): 0.1, ("Q", "second"): 0.99},
            threshold=0.9,
            lambda_=0.1,
        )
        outcome = router.route(request())
        self.assertEqual(outcome.models_called, ["first"])
        self.assertNotIn("second", codes(router))
        self.assertTrue(any(event["event"] == "budget_stop" for event in outcome.trace))

    def test_cheapest_over_budget_calls_nobody(self):
        router = make_router([model("skiff", per_request=2.0)], dollars=1.0)
        before = len(router.provider.calls)
        outcome = router.route(request())
        self.assertEqual(len(router.provider.calls), before)
        self.assertEqual(outcome.problem["failure_class"], "budget_exhausted")
        self.assertEqual(outcome.http_status, outcome.problem["status"])
        self.assertEqual(outcome.problem["status"], 402)
        self.assertEqual(outcome.content_type, "application/problem+json")

    def test_oversized_input_calls_nobody(self):
        router = make_router([model("skiff", per_request=0.1)], max_input_tokens=10)
        outcome = router.route(request(tokens=11))
        self.assertEqual(router.provider.calls, [])
        self.assertEqual(outcome.stop_reason, "input_cap")
        self.assertEqual(outcome.problem["status"], 413)
        wide = make_router([model("skiff", per_request=0.1)], max_input_bytes=4)
        outcome = wide.route(request(text="abcdef"))
        self.assertEqual(wide.provider.calls, [])
        self.assertEqual(outcome.problem["status"], 413)

    def test_caller_budgets_are_independent(self):
        other = CallerAccount(caller_id="desk-beta", dollars=5.0)
        router = make_router(
            [model("skiff", per_request=1.0)],
            dollars=1.0,
            extra_accounts={"desk-beta": other},
        )
        first = router.route(request(query_id="A", text="notice A"))
        second = router.route(request(query_id="B", text="notice B"))
        beta = router.route(request(query_id="C", text="notice C", caller="desk-beta"))
        self.assertTrue(first.ok)
        self.assertEqual(second.problem["failure_class"], "budget_exhausted")
        self.assertTrue(beta.ok)
        self.assertAlmostEqual(other.dollars, 4.0)

    def test_quota_and_anomaly(self):
        router = make_router(
            [model("skiff", per_request=0.1)],
            request_quota=1,
            anomaly_rate=1,
        )
        router.route(request(query_id="A", text="notice A"))
        blocked = router.route(request(query_id="B", text="notice B"))
        self.assertEqual(blocked.problem["failure_class"], "caller_quota")
        self.assertEqual(len(router.provider.calls), 1)
        self.assertTrue(any(item["kind"] == "caller_rate_exceeded" for item in router.anomalies))

        other = CallerAccount(caller_id="desk-beta", dollars=5.0, request_quota=5, anomaly_rate=5)
        shared = make_router(
            [model("skiff", per_request=0.1)],
            dollars=5.0,
            request_quota=1,
            extra_accounts={"desk-beta": other},
        )
        shared.route(request(text="one"))
        shared.route(request(query_id="B", text="two"))
        beta_before = other.dollars
        shared.route(request(query_id="C", text="three", caller="desk-beta"))
        self.assertAlmostEqual(other.dollars, beta_before - 0.1)

    def test_success_cache_and_rejected_statuses(self):
        router = make_router([model("skiff", per_request=0.25)])
        first = router.route(request(text="berth  window"))
        second = router.route(request(query_id="Q2", text="berth window"))
        self.assertTrue(first.ok)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.charged, 0.0)
        self.assertEqual(len(router.provider.calls), 1)
        third = router.route(request(query_id="Q3", text="berth window", prompt_version="slip-v2"))
        self.assertFalse(third.from_cache)
        self.assertEqual(len(router.provider.calls), 2)

        limited = make_router(
            [model("skiff", per_request=0.1)],
            scripts={
                "skiff": [
                    ScriptStep(status=429, error_code="slow_down", error_type="rate_limit_error"),
                    ScriptStep(status=200, request_id="req-ok"),
                ]
            },
            max_attempts=1,
        )
        missed = limited.route(request(text="same notice"))
        self.assertEqual(len(limited.cache), 0)
        self.assertIn(429, UNCACHEABLE_STATUSES)
        self.assertFalse(missed.ok)
        landed = limited.route(request(query_id="later", text="same notice"))
        self.assertTrue(landed.ok)
        cached = limited.route(request(query_id="again", text="same notice"))
        self.assertTrue(cached.from_cache)
        self.assertEqual(len(limited.provider.calls), 2)

    def test_billing_does_not_failover_the_same_key(self):
        models = [
            model("gpt-a", credential="org-1", per_request=0.2, ex_ante={"Q": 0.9}),
            model("gpt-b", credential="org-1", per_request=0.2, ex_ante={"Q": 0.8}),
        ]
        router = make_router(
            models,
            scripts={
                "gpt-a": [
                    ScriptStep(
                        status=429,
                        error_code="credit_balance_exhausted",
                        error_type="insufficient_quota",
                        error_message="secret-key-should-not-leak",
                    )
                ],
                "gpt-b": [ScriptStep()],
            },
            lambda_=0.1,
        )
        outcome = router.route(request())
        self.assertEqual(codes(router), ["gpt-a"])
        self.assertEqual(outcome.problem["failure_class"], "terminal_billing")
        self.assertEqual(router.clock.sleeps, [])
        self.assertNotIn("secret-key-should-not-leak", json.dumps(outcome.problem))

    def test_billing_can_use_a_different_credential(self):
        models = [
            model("gpt-a", provider="openai", credential="org-1", per_request=0.2, ex_ante={"Q": 0.9}),
            model("claude-a", provider="anthropic", credential="org-2", per_request=0.2, ex_ante={"Q": 0.8}),
        ]
        router = make_router(
            models,
            scripts={
                "gpt-a": [
                    ScriptStep(
                        status=429,
                        error_code="project_spend_limit_exceeded",
                        error_type="insufficient_quota",
                    )
                ]
            },
            lambda_=0.1,
        )
        outcome = router.route(request())
        self.assertEqual(outcome.models_called, ["gpt-a", "claude-a"])
        self.assertTrue(outcome.ok)
        self.assertEqual(router.clock.sleeps, [])

    def test_slow_down_waits_for_retry_after(self):
        router = make_router(
            [model("skiff", per_request=0.1)],
            scripts={
                "skiff": [
                    ScriptStep(
                        status=429,
                        error_code="slow_down",
                        error_type="rate_limit_error",
                        headers={"Retry-After": "2"},
                    ),
                    ScriptStep(status=200),
                ]
            },
        )
        outcome = router.route(request())
        self.assertTrue(outcome.ok)
        self.assertGreaterEqual(router.clock.now_ms, 2000.0)
        self.assertTrue(any(item >= 2000.0 for item in router.clock.sleeps))

    def test_openai_503_is_not_a_429(self):
        router = make_router(
            [model("skiff")],
            scripts={
                "skiff": [
                    ScriptStep(
                        status=503,
                        error_code="server_is_overloaded",
                        error_type="service_unavailable_error",
                    )
                ]
            },
            max_attempts=3,
        )
        outcome = router.route(request())
        self.assertEqual(len(router.provider.calls), 3)
        self.assertEqual(outcome.problem["failure_class"], "overloaded")
        self.assertEqual(outcome.problem["status"], 503)
        self.assertNotEqual(outcome.problem["status"], 429)

    def test_claude_413_does_not_send_the_same_body_elsewhere(self):
        models = [
            model("wide", provider="anthropic", per_request=0.1, ex_ante={"Q": 0.9}),
            model("other", provider="openai", per_request=1.0, ex_ante={"Q": 0.4}),
        ]
        router = make_router(
            models,
            scripts={
                "wide": [ScriptStep(status=413, error_type="request_too_large")]
            },
            lambda_=0.1,
        )
        outcome = router.route(request())
        self.assertEqual(codes(router), ["wide"])
        self.assertEqual(outcome.problem["failure_class"], "request_defect")

    def test_claude_529_can_failover(self):
        models = [
            model("hot", provider="anthropic", per_request=0.2, ex_ante={"Q": 0.95}),
            model("cool", provider="openai", per_request=0.3, ex_ante={"Q": 0.9}),
        ]
        router = make_router(
            models,
            scripts={
                "hot": [ScriptStep(status=529, error_type="overloaded_error")]
            },
            lambda_=0.1,
            max_attempts=1,
        )
        outcome = router.route(request())
        self.assertEqual(outcome.models_called, ["hot", "cool"])
        self.assertTrue(outcome.ok)
        classes = [event.get("failure_class") for event in outcome.trace]
        self.assertIn("overloaded", classes)
        self.assertNotIn("request_defect", classes)

    def test_claude_bare_429_second_hit_is_terminal(self):
        router = make_router(
            [model("claude", provider="anthropic", per_request=0.1)],
            scripts={
                "claude": [
                    ScriptStep(status=429, error_type="rate_limit_error"),
                ]
            },
            max_attempts=3,
        )
        outcome = router.route(request())
        self.assertEqual(len(router.provider.calls), 2)
        self.assertEqual(outcome.problem["failure_class"], "terminal_billing")

    def test_claude_429_with_retry_after_backs_off(self):
        router = make_router(
            [model("claude", provider="anthropic", per_request=0.1)],
            scripts={
                "claude": [
                    ScriptStep(
                        status=429,
                        error_type="rate_limit_error",
                        headers={"retry-after": "2"},
                    ),
                    ScriptStep(status=200),
                ]
            },
        )
        outcome = router.route(request())
        self.assertTrue(outcome.ok)
        self.assertGreaterEqual(sum(router.clock.sleeps), 2000.0)

    def test_claude_spend_limit_400_is_terminal(self):
        models = [
            model("claude", provider="anthropic", credential="org", per_request=0.1, ex_ante={"Q": 0.9}),
            model("other", provider="anthropic", credential="org", per_request=0.1, ex_ante={"Q": 0.8}),
        ]
        router = make_router(
            models,
            scripts={
                "claude": [ScriptStep(status=400, spend_limit=True, error_type="invalid_request_error")]
            },
            lambda_=0.1,
        )
        outcome = router.route(request())
        self.assertEqual(codes(router), ["claude"])
        self.assertEqual(outcome.problem["failure_class"], "terminal_billing")
        self.assertEqual(router.clock.sleeps, [])

    def test_sse_after_200_is_not_success_or_cached(self):
        router = make_router(
            [
                model(
                    "skiff",
                    input_per_token=0.25,
                    output_per_token=0.5,
                    expected_output_tokens=0,
                )
            ],
            scripts={
                "skiff": [
                    ScriptStep(
                        status=200,
                        sse_error=True,
                        input_tokens=4,
                        output_tokens=6,
                        text='{"partial":true}',
                    )
                ]
            },
            dollars=10.0,
        )
        outcome = router.route(request(tokens=4))
        self.assertFalse(outcome.ok)
        self.assertEqual(len(router.cache), 0)
        self.assertAlmostEqual(outcome.charged, 0.5 * 6 + 0.25 * 4)

    def test_timeout_reserves_the_estimate(self):
        router = make_router(
            [
                model(
                    "skiff",
                    input_per_token=0.5,
                    output_per_token=0.25,
                    expected_output_tokens=4,
                )
            ],
            scripts={
                "skiff": [
                    ScriptStep(timeout=True, usage_present=False, status=504, error_type="timeout_error")
                ]
            },
            dollars=5.0,
        )
        outcome = router.route(request(tokens=2))
        self.assertEqual(len(router.provider.calls), 1)
        self.assertAlmostEqual(outcome.charged, 2.0)
        self.assertEqual(outcome.problem["failure_class"], "timeout")
        self.assertEqual(len(router.cache), 0)

    def test_network_511_is_not_cached(self):
        router = make_router(
            [model("skiff", per_request=0.1)],
            scripts={"skiff": [ScriptStep(status=511)]},
            max_attempts=1,
        )
        first = router.route(request(text="login wall"))
        second = router.route(request(query_id="again", text="login wall"))
        self.assertEqual(first.problem["failure_class"], "network")
        self.assertEqual(len(router.cache), 0)
        self.assertEqual(len(router.provider.calls), 2)
        self.assertFalse(second.ok)

    def test_rfc9110_archive_gap_non_integer_retry_after_uses_jitter(self):
        router = make_router(
            [model("skiff")],
            scripts={
                "skiff": [
                    ScriptStep(
                        status=429,
                        error_code="slow_down",
                        error_type="rate_limit_error",
                        headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
                    ),
                    ScriptStep(status=200),
                ]
            },
            rng=SeqRNG([0.0]),
            gamma=0.0,
        )
        outcome = router.route(request())
        self.assertTrue(outcome.ok)
        sleeps = [event for event in outcome.trace if event["event"] == "retry_sleep"]
        self.assertEqual(sleeps[0]["retry_after_parse"], "absent_or_unparsed")
        self.assertEqual(sleeps[0]["sleep_ms"], 0.0)
        self.assertEqual(router.clock.now_ms, 0.0)

    def test_retry_after_thirty_seconds_is_not_clamped(self):
        router = make_router(
            [model("skiff")],
            scripts={
                "skiff": [
                    ScriptStep(status=500, headers={"Retry-After": "30"}),
                    ScriptStep(status=200),
                ]
            },
            rng=SeqRNG([0.0]),
        )
        router.route(request())
        self.assertGreaterEqual(router.clock.now_ms, 30000.0)

    def test_aws_timeout_retries_and_validation_does_not(self):
        retried = make_router(
            [model("awsbox", provider="aws")],
            scripts={
                "awsbox": [
                    ScriptStep(status=400, error_code="RequestTimeout"),
                    ScriptStep(status=200),
                ]
            },
        )
        outcome = retried.route(request())
        self.assertTrue(outcome.ok)
        self.assertEqual(len(retried.provider.calls), 2)
        self.assertTrue(retried.clock.sleeps)

        rejected = make_router(
            [
                model("awsbox", provider="aws", per_request=0.1, ex_ante={"Q": 0.9}),
                model("other", per_request=0.2, ex_ante={"Q": 0.2}),
            ],
            scripts={
                "awsbox": [ScriptStep(status=400, error_code="ValidationException")]
            },
            lambda_=0.1,
        )
        outcome = rejected.route(request())
        self.assertEqual(codes(rejected), ["awsbox"])
        self.assertEqual(outcome.problem["failure_class"], "request_defect")
        self.assertEqual(rejected.clock.sleeps, [])

    def test_throttling_code_uses_the_long_base(self):
        router = make_router(
            [model("awsbox", provider="aws")],
            scripts={
                "awsbox": [
                    ScriptStep(status=503, error_code="ThrottlingException"),
                    ScriptStep(status=200),
                ]
            },
            rng=SeqRNG([0.999]),
        )
        router.route(request())
        self.assertGreater(router.clock.sleeps[0], 50.0)
        self.assertLess(router.clock.sleeps[0], 1000.0)

    def test_bucket_blocks_a_retry_without_sleep_while_dollars_remain(self):
        router = make_router(
            [model("skiff", per_request=0.0)],
            scripts={"skiff": [ScriptStep(status=500)]},
            bucket_balance=14,
            dollars=5.0,
            max_attempts=3,
        )
        outcome = router.route(request())
        self.assertFalse(outcome.ok)
        self.assertEqual(router.bucket.balance, 0)
        self.assertEqual(len(router.clock.sleeps), 1)
        self.assertTrue(any(event["event"] == "retry_blocked" for event in outcome.trace))
        self.assertGreater(router.accounts["desk-alpha"].dollars, 0.0)

    def test_successful_retry_nets_minus_14(self):
        router = make_router(
            [model("skiff")],
            scripts={
                "skiff": [
                    ScriptStep(status=500),
                    ScriptStep(status=500),
                    ScriptStep(status=200),
                ]
            },
            bucket_balance=500,
        )
        router.route(request())
        self.assertEqual(router.bucket.balance, 486)

    def test_expensive_successes_drain_dollars_and_leave_the_bucket_full(self):
        router = make_router(
            [model("skiff", per_request=2.0)],
            dollars=4.0,
            bucket_balance=500,
        )
        self.assertTrue(router.route(request(query_id="A", text="notice A")).ok)
        self.assertTrue(router.route(request(query_id="B", text="notice B")).ok)
        blocked = router.route(request(query_id="C", text="notice C"))
        self.assertEqual(blocked.problem["failure_class"], "budget_exhausted")
        self.assertEqual(len(router.provider.calls), 2)
        self.assertEqual(router.bucket.balance, 500)

    def test_attempt_caps(self):
        storm = make_router(
            [model("skiff")],
            scripts={"skiff": [ScriptStep(status=503, error_code="server_is_overloaded")]},
            max_attempts=3,
            max_total_attempts=9,
        )
        storm.route(request())
        self.assertEqual(len(storm.provider.calls), 3)

        capped = make_router(
            [model("skiff")],
            scripts={"skiff": [ScriptStep(status=500)]},
            max_attempts=5,
            max_total_attempts=2,
        )
        outcome = capped.route(request())
        self.assertEqual(outcome.http_calls, 2)
        self.assertTrue(any(event["event"] == "attempt_cap" for event in outcome.trace))

    def test_invalid_slip_is_not_cached(self):
        models = [
            model("bad", per_request=0.1, ex_ante={"Q": 0.95}),
            model("good", per_request=0.2, ex_ante={"Q": 0.9}),
        ]
        router = make_router(
            models,
            scripts={"bad": [ScriptStep(text="not-json")]},
            scores={("Q", "bad"): 1.0, ("Q", "good"): 1.0},
            lambda_=0.1,
        )
        outcome = router.route(request())
        self.assertEqual(outcome.model_id, "good")
        self.assertIsNone(router.cache.get("bad", "slip-v1", "notice for MV EXAMPLE"))
        self.assertIsNotNone(outcome.slip)

    def test_conflict_is_not_replayed_immediately(self):
        router = make_router(
            [model("claude", provider="anthropic")],
            scripts={
                "claude": [
                    ScriptStep(status=409, error_type="conflict_error"),
                    ScriptStep(status=200),
                ]
            },
        )
        first = router.route(request())
        self.assertEqual(first.problem["failure_class"], "conflict")
        self.assertEqual(len(router.provider.calls), 1)
        self.assertEqual(router.clock.sleeps, [])
        blind = router.route(request(query_id="replay"))
        self.assertEqual(blind.problem["failure_class"], "conflict")
        self.assertEqual(blind.problem["status"], 409)
        self.assertEqual(len(router.provider.calls), 1)
        self.assertTrue(any(event["event"] == "conflict_pending" for event in blind.trace))
        second = router.route(request(query_id="retry", conflict_resolved=True))
        self.assertTrue(second.ok)
        self.assertEqual(len(router.provider.calls), 2)
        self.assertTrue(any(event["event"] == "conflict_cleared" for event in second.trace))

    def test_consumption_spike_is_logged(self):
        router = make_router(
            [model("skiff", per_request=1.0)],
            anomaly_spend=0.5,
        )
        router.route(request())
        self.assertTrue(any(item["kind"] == "consumption_spike" for item in router.anomalies))

    def test_claude_bare_429_chain_resets_after_success(self):
        router = make_router(
            [model("claude", provider="anthropic", per_request=0.1)],
            scripts={
                "claude": [
                    ScriptStep(status=429, error_type="rate_limit_error"),
                    ScriptStep(status=200),
                    ScriptStep(status=429, error_type="rate_limit_error"),
                    ScriptStep(status=200),
                ]
            },
        )
        self.assertTrue(router.route(request()).ok)
        later = router.route(request(query_id="Q2", text="another notice"))
        self.assertTrue(later.ok)
        self.assertNotIn("claude", router.blocked_credentials)
        rules = [event.get("lab_rule") for event in later.trace if event["event"] == "attempt"]
        self.assertIn("first_bare_429", rules)
        self.assertNotIn("second_bare_429", rules)

    def test_all_invalid_slips_return_contract_invalid(self):
        router = make_router(
            [model("bad", per_request=0.1)],
            scripts={"bad": [ScriptStep(text='{"berth":"B-4"}')]},
        )
        outcome = router.route(request())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.problem["failure_class"], "contract_invalid")
        self.assertEqual(outcome.problem["status"], 422)
        self.assertEqual(outcome.http_status, 422)
        self.assertAlmostEqual(outcome.charged, 0.1)
        self.assertEqual(len(router.cache), 0)

    def test_claude_504_without_usage_reserves_the_estimate(self):
        router = make_router(
            [
                model(
                    "claude",
                    provider="anthropic",
                    input_per_token=0.5,
                    output_per_token=0.25,
                    expected_output_tokens=4,
                )
            ],
            scripts={
                "claude": [ScriptStep(status=504, error_type="timeout_error", usage_present=False)]
            },
        )
        outcome = router.route(request(tokens=2))
        self.assertEqual(outcome.problem["failure_class"], "timeout")
        self.assertAlmostEqual(outcome.charged, 0.5 * 2 + 0.25 * 4)
        self.assertEqual(len(router.provider.calls), 1)

    def test_retry_that_cannot_fit_the_budget_costs_no_tokens_or_sleep(self):
        router = make_router(
            [model("skiff", per_request=1.0)],
            scripts={"skiff": [ScriptStep(status=500)]},
            dollars=1.5,
        )
        outcome = router.route(request())
        self.assertFalse(outcome.ok)
        self.assertEqual(len(router.provider.calls), 1)
        self.assertEqual(router.bucket.balance, 500)
        self.assertEqual(router.clock.sleeps, [])
        self.assertTrue(any(event["event"] == "budget_stop" for event in outcome.trace))

    def test_problem_detail_names_the_vendor_code_not_the_message(self):
        router = make_router(
            [model("skiff", per_request=0.1)],
            scripts={
                "skiff": [
                    ScriptStep(
                        status=400,
                        error_code="service_tier",
                        error_message="secret-key-should-not-leak",
                    )
                ]
            },
        )
        outcome = router.route(request())
        self.assertEqual(outcome.problem["detail"], "skiff service_tier classified as request_defect")
        self.assertNotIn("secret-key-should-not-leak", json.dumps(outcome.problem))

    def test_cache_hit_is_per_model(self):
        models = [
            model("first", per_request=0.1, ex_ante={"Q": 0.9}),
            model("second", per_request=0.1, ex_ante={"Q": 0.8}),
        ]
        router = make_router(models, lambda_=0.1)
        self.assertEqual(router.route(request()).model_id, "first")
        router.blocked_models.add("first")
        other = router.route(request(query_id="Q"))
        self.assertEqual(other.model_id, "second")
        self.assertFalse(other.from_cache)
        self.assertEqual(codes(router), ["first", "second"])

    def test_notice_fixtures_are_closed_slips(self):
        payload = json.loads((EXAMPLES / "notices.json").read_text(encoding="utf-8"))
        for notice in payload["notices"]:
            self.assertEqual(parse_slip(json.dumps(notice["slip"]))["vessel"].split()[0], "MV")


if __name__ == "__main__":
    unittest.main()
