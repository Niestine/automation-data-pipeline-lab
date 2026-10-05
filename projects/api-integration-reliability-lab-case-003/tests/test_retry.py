"""Full jitter, Retry-After, and a local one-row OCC comparison."""

from __future__ import annotations

import random
import unittest

import helpers
from plot_allotment.httpmsg import Response
from plot_allotment.journal import Journal
from plot_allotment.occsim import compare_occ
from plot_allotment.retry import (
    RetryPolicy,
    call_with_retry,
    equal_jitter,
    expo,
    full_jitter,
)

PARENT = helpers.PARENT


class RetryTests(unittest.TestCase):
    def test_full_jitter_stays_inside_the_cap(self) -> None:
        rng = random.Random(9)
        self.assertEqual(expo(0.05, 2.0, 20), 2.0)
        for attempt in (0, 1, 8, 40, 80):
            for _ in range(40):
                delay = full_jitter(attempt, 0.05, 2.0, rng)
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, 2.0)
        for _ in range(30):
            delay = equal_jitter(20, 0.05, 2.0, rng)
            self.assertGreaterEqual(delay, 1.0)
            self.assertLessEqual(delay, 2.0)

    def test_retry_after_is_not_added_to_the_cap_and_422_is_once(self) -> None:
        lab = helpers.Lab(seed=6)
        self.addCleanup(lab.close)
        lab.add(helpers.lot("lot-01", 1), helpers.lot("lot-02", 2), helpers.lot("lot-03", 3))
        lab.service.fail_list = [(503, "2")]
        rows = lab.client.walk(PARENT, page_size=10)
        self.assertEqual(len(rows), 3)
        self.assertEqual(lab.sleeps, [2.0])
        self.assertGreaterEqual(lab.sleeps[0], 2)
        self.assertLess(lab.sleeps[0], 4)

        body = helpers.lot("lot-08", 8)
        created = lab.client.upsert(PARENT, body, "55555555-5555-4555-8555-555555555555")
        self.assertEqual(created.status, 200)
        calls = lab.client.calls
        rejected = lab.client.upsert(
            PARENT,
            helpers.lot("lot-08", 8, note="revised"),
            "55555555-5555-4555-8555-555555555555",
        )
        self.assertEqual(rejected.status, 422)
        self.assertEqual(lab.client.calls, calls + 1)

    def test_http_date_retry_after_falls_back_to_full_jitter(self) -> None:
        lab = helpers.Lab(seed=11)
        self.addCleanup(lab.close)
        lab.add(helpers.lot("lot-01", 1))
        lab.service.fail_list = [(503, "Wed, 21 Oct 2015 07:28:00 GMT")]
        rows = lab.client.walk(PARENT, page_size=10)
        self.assertEqual([row["resource_id"] for row in rows], ["lot-01"])
        self.assertEqual(len(lab.sleeps), 1)
        self.assertGreaterEqual(lab.sleeps[0], 0.0)
        self.assertLessEqual(lab.sleeps[0], 2.0)
        self.assertTrue(lab.journal.has("client_status_503", "full_jitter_retry"))

    def test_client_retries_409_without_changing_the_request(self) -> None:
        journal = Journal()
        sleeps: list[float] = []
        responses = [
            Response(409, {"content-type": "application/problem+json"}, b"{}"),
            Response(200, {"content-type": "application/json"}, b"{}"),
        ]

        def operation() -> Response:
            return responses.pop(0)

        result = call_with_retry(
            operation,
            RetryPolicy(),
            random.Random(1),
            sleeps.append,
            journal,
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(len(responses), 0)
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(journal.has("client_status_409", "full_jitter_retry"))

    def test_occ_full_jitter_writes_less_than_no_backoff(self) -> None:
        totals = compare_occ(clients=12, trials=8, seed=20261006)
        for name in ("no_backoff", "exponential", "equal_jitter", "full_jitter"):
            self.assertGreater(totals[name], 0)
        self.assertLess(totals["full_jitter"], totals["no_backoff"])


if __name__ == "__main__":
    unittest.main()
