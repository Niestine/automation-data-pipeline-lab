import unittest

import helpers  # noqa: F401
from helpers import catalog_dict, job_dict, make_runner

from automation_job_lab.errors import JobTimeout, SchemaError, TransientError, ValidationError
from automation_job_lab.retry import RetryPolicy, backoff_ms, is_retryable, rng_for


class RetryTests(unittest.TestCase):
    def test_backoff_doubles_until_cap(self):
        policy = RetryPolicy(base_delay_ms=10, max_delay_ms=50, multiplier=2.0, jitter_ms=0)
        rng = rng_for(7, "ingest-inbox")
        delays = [backoff_ms(policy, attempt, rng) for attempt in range(1, 6)]
        self.assertEqual(delays, [10, 20, 40, 50, 50])

    def test_jitter_is_deterministic_for_the_same_seed(self):
        policy = RetryPolicy(base_delay_ms=10, jitter_ms=3)
        first = [backoff_ms(policy, n, rng_for(7, n, "ingest")) for n in (1, 2, 3)]
        second = [backoff_ms(policy, n, rng_for(7, n, "ingest")) for n in (1, 2, 3)]
        self.assertEqual(first, second)
        for base, delay in zip((10, 20, 40), first):
            self.assertGreaterEqual(delay, base)
            self.assertLessEqual(delay, base + 3)

    def test_retryable_classification(self):
        self.assertTrue(is_retryable(JobTimeout("x")))
        self.assertTrue(is_retryable(TransientError("x")))
        self.assertFalse(is_retryable(ValidationError("x")))
        self.assertFalse(is_retryable(SchemaError("x")))

    def test_timeout_retries_then_succeeds(self):
        catalog = catalog_dict(
            job_dict(
                id="heartbeat-log",
                retry={
                    "max_attempts": 3,
                    "base_delay_ms": 10,
                    "max_delay_ms": 200,
                    "multiplier": 2.0,
                    "jitter_ms": 0,
                },
            )
        )
        runner, _, sleeper, logger, _, _, _ = make_runner(
            catalog=catalog,
            inbox=[],
            faults=[{"job_id": "heartbeat-log", "attempt": 1, "error": "timeout"}],
        )
        report = runner.run()
        result = report.job_map()["heartbeat-log"]
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(sleeper.delays, [10])
        self.assertEqual(report.retries, 1)
        self.assertEqual(len(logger.of_type("job_retry")), 1)

    def test_exhausted_retries_fail_the_job(self):
        catalog = catalog_dict(
            job_dict(
                id="heartbeat-log",
                retry={
                    "max_attempts": 2,
                    "base_delay_ms": 5,
                    "max_delay_ms": 5,
                    "multiplier": 2.0,
                    "jitter_ms": 0,
                },
            )
        )
        runner, _, sleeper, _, _, _, _ = make_runner(
            catalog=catalog,
            inbox=[],
            faults=[
                {"job_id": "heartbeat-log", "attempt": 1, "error": "transient_io"},
                {"job_id": "heartbeat-log", "attempt": 2, "error": "transient_io"},
            ],
        )
        report = runner.run()
        result = report.job_map()["heartbeat-log"]
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "transient_io")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(sleeper.delays, [5])
        self.assertEqual(report.status, "failed")

    def test_validation_fault_is_not_retried(self):
        catalog = catalog_dict(job_dict(id="heartbeat-log"))
        runner, _, sleeper, logger, _, _, _ = make_runner(
            catalog=catalog,
            inbox=[],
            faults=[{"job_id": "heartbeat-log", "attempt": 1, "error": "validation"}],
        )
        report = runner.run()
        result = report.job_map()["heartbeat-log"]
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "validation_error")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(sleeper.delays, [])
        self.assertEqual(logger.of_type("job_retry"), [])
