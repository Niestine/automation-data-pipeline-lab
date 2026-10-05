"""A paused holder cannot complete or renew after a higher fence is stored."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

import helpers
from shiftlease.errors import EffectExhausted


class FenceTests(unittest.TestCase):
    def test_stale_completion_and_event_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            store.submit("slip-2", helpers.slip(sku="COAT7", qty=1, bin_code="B12"))
            worker_a, effect, logger, _sleeper = helpers.make_worker(store, root / "out", owner="holder-a")
            self.assertEqual(worker_a.run_once(), "job")
            first = store.job_by_key("slip-1")
            assert first is not None
            self.assertEqual(first["status"], "succeeded")
            events = store.events(first["id"])
            self.assertEqual([item["kind"] for item in events], ["claim", "complete"])
            self.assertEqual(events[0]["fence"], events[1]["fence"])
            self.assertLess(events[0]["seq"], events[1]["seq"])
            self.assertTrue(all("fence" in record and "owner" in record and "seq" in record for record in logger.records))

            store.shift_clock(0)
            paused = store.claim("holder-a", random.Random(3))
            self.assertEqual(paused.idempotency_key, "slip-2")
            intent = store.ensure_intent(paused.job_id, "holder-a", paused.fence, paused.idempotency_key)
            assert intent is not None
            effect.perform(idempotency_key=paused.idempotency_key, payload=paused.payload, fence=paused.fence)
            store.shift_clock(31)
            stolen = store.claim("holder-b", random.Random(4))
            self.assertEqual(stolen.fence, paused.fence + 1)
            self.assertFalse(
                store.complete(paused.job_id, "holder-a", paused.fence, paused.idempotency_key, {"path": "stale"})
            )
            self.assertFalse(store.renew(paused.job_id, "holder-a", paused.fence))
            self.assertFalse(
                store.mark_applied(paused.job_id, "holder-a", paused.fence, paused.idempotency_key, {"path": "stale"})
            )
            self.assertEqual(store.intent(paused.idempotency_key)["state"], "pending")
            worker_b, _effect_b, _logger_b, _sleeper_b = helpers.make_worker(
                store, root / "out", owner="holder-b", seed=4
            )
            worker_b.resume_held()
            final = store.job(paused.job_id)
            assert final is not None
            self.assertEqual(final["status"], "succeeded")
            self.assertEqual(final["fence"], stolen.fence)
            self.assertEqual(final["owner"], "holder-b")
            applied = store.intent(paused.idempotency_key)
            assert applied is not None
            self.assertEqual(applied["state"], "applied")
            self.assertEqual(len(effect.created), 2)
            kinds = [(item["fence"], item["kind"]) for item in store.events(paused.job_id)]
            self.assertIn((paused.fence, "claim"), kinds)
            self.assertIn((stolen.fence, "claim"), kinds)
            self.assertIn((stolen.fence, "complete"), kinds)
            self.assertNotIn((paused.fence, "complete"), kinds)
            store.close()

    def test_failed_renewal_starts_no_further_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            worker, effect, _logger, _sleeper = helpers.make_worker(store, root / "out")
            claimed = store.claim("holder-a", random.Random(1))
            worker.current = claimed
            store.shift_clock(31)
            self.assertFalse(worker.renew_current())
            self.assertTrue(worker.jeopardy)
            worker.process(claimed)
            self.assertEqual(effect.calls, [])
            self.assertEqual(worker.effects_started, 0)
            self.assertEqual(store.job(claimed.job_id)["status"], "leased")
            store.close()

    def test_jeopardy_between_retries_does_not_call_the_effect_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30, effect_retry_cap=2, jitter_cap_seconds=0.0)
            store.submit("slip-1", helpers.slip())
            holder: dict = {}

            class PauseEffect:
                def __init__(self) -> None:
                    self.calls = 0

                def perform(self, **_kwargs: object) -> dict:
                    self.calls += 1
                    holder["worker"].jeopardy = True
                    raise RuntimeError("paused")

            effect = PauseEffect()
            from shiftlease.logjson import JsonLogger
            from shiftlease.worker import Worker
            import random as rng_mod

            worker = Worker(
                store,
                effect,  # type: ignore[arg-type]
                "holder-a",
                store.config,
                rng_mod.Random(1),
                lambda _delay: None,
                JsonLogger(),
            )
            holder["worker"] = worker
            claimed = store.claim("holder-a", rng_mod.Random(1))
            with self.assertRaises(EffectExhausted):
                worker._perform(claimed)
            self.assertEqual(effect.calls, 1)
            self.assertEqual(worker.effects_blocked_by_jeopardy, 1)
            store.close()
