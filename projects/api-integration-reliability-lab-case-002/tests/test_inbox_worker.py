"""Durable inbox, short replay cache, worker memo, and crash recovery."""

from __future__ import annotations

import threading
import unittest

import helpers
from quay_inbox.errors import SchemaError, SimulatedCrash
from quay_inbox.horizons import (
    CURRENT_API_VERSION,
    INBOX_RETENTION_SECONDS,
    SPEC_TABLE_HORIZON,
    STRIPE_AUTOMATIC_HORIZON,
)
from quay_inbox.receiver import Receiver
from quay_inbox.schema import filter_subscription, validate_snapshot
from quay_inbox.secrets import DASHBOARD_SECRET, Route
from quay_inbox.store import Store
from quay_inbox.worker import Downstream, Upstream, Worker


class CheckThenActStore(Store):
    """Negative control: reads the inbox outside the lock, then inserts."""

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self.barrier = barrier

    def claim(self, row):
        with self.lock:
            exists = row.event_id in self.inbox
        self.barrier.wait()
        if exists:
            self.claim_duplicates += 1
            return "duplicate"
        with self.lock:
            self.inbox[row.event_id] = row
            self.claim_inserts += 1
        return "inserted"


def _stack(start: int = 1_700_000_100, **receiver_kwargs):
    store, clock = helpers.make_store_clock(start)
    route = helpers.standard_route()
    receiver = Receiver(store, clock, [route], **receiver_kwargs)
    upstream = Upstream({})
    downstream = Downstream()
    worker = Worker(store, upstream, downstream)
    return store, clock, receiver, upstream, downstream, worker


class InboxTests(unittest.TestCase):
    def test_short_cache_suppresses_the_far_edge_and_inbox_keeps_later_retries(self) -> None:
        store, clock, receiver, _upstream, downstream, worker = _stack()
        body = helpers.standard_body()
        timestamp = clock.time() + 300
        request = helpers.signed_request(body, timestamp=timestamp)
        self.assertEqual(receiver.handle(request).status, 200)
        worker.drain()
        self.assertEqual(store.memo_misses, 1)
        clock.advance(600)
        replay = helpers.signed_request(body, timestamp=timestamp)
        self.assertEqual(receiver.handle(replay).status, 200)
        self.assertEqual(receiver.cache_suppressions, 1)
        worker.drain()
        self.assertEqual(store.memo_misses, 1)
        self.assertEqual(len(downstream.applied), 1)

        store.forget_fresh()
        clock.advance(76 * 3600)
        later = helpers.signed_request(body, timestamp=clock.time(), event_id="msg_quay_0001")
        self.assertEqual(receiver.handle(later).status, 200)
        worker.drain()
        self.assertEqual(store.memo_misses, 1)

        clock.advance(3 * 24 * 3600)
        three_days = helpers.signed_request(body, timestamp=clock.time())
        self.assertEqual(receiver.handle(three_days).status, 200)
        worker.drain()
        self.assertEqual(store.memo_misses, 1)
        self.assertLess(SPEC_TABLE_HORIZON, 76 * 3600)
        self.assertLess(STRIPE_AUTOMATIC_HORIZON, INBOX_RETENTION_SECONDS)
        self.assertTrue(store.retained(1_700_000_100, 1_700_000_100 + INBOX_RETENTION_SECONDS))
        self.assertFalse(store.retained(1_700_000_100, 1_700_000_100 + INBOX_RETENTION_SECONDS + 1))

    def test_five_minute_store_applies_the_hour_later_retry_twice(self) -> None:
        store, clock, receiver, _upstream, _downstream, worker = _stack()
        body = helpers.standard_body()
        first = helpers.signed_request(body, timestamp=clock.time())
        self.assertEqual(receiver.handle(first).status, 200)
        worker.drain()
        self.assertEqual(store.memo_misses, 1)
        clock.advance(300)
        store.inbox.clear()
        store.memos.clear()
        store.outbox.clear()
        store.releases.clear()
        store.pair_seen.clear()
        store.last_created.clear()
        store.forget_fresh()
        clock.advance(3600)
        retry = helpers.signed_request(body, timestamp=clock.time())
        self.assertEqual(receiver.handle(retry).status, 200)
        worker.drain()
        self.assertEqual(store.memo_misses, 2)
        self.assertEqual(store.release_changes, 2)

    def test_concurrent_claim_is_one_effect_and_check_then_act_is_not(self) -> None:
        store, clock, receiver, _upstream, _downstream, worker = _stack()
        body = helpers.standard_body()
        request = helpers.signed_request(body, timestamp=clock.time())
        barrier = threading.Barrier(2)

        def run() -> None:
            barrier.wait()
            receiver.handle(request)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        worker.drain()
        self.assertEqual(store.claim_inserts, 1)
        self.assertEqual(store.memo_misses, 1)

        self.assertEqual(receiver.accepted, 1)
        self.assertEqual(receiver.duplicates, 1)

        # The same race against a claim that checks outside the lock: both
        # requests believe they inserted, so both are enqueued as new.
        racy = CheckThenActStore(threading.Barrier(2))
        racy_receiver = Receiver(racy, clock, [helpers.standard_route()])
        racers = [threading.Thread(target=racy_receiver.handle, args=(request,)) for _ in range(2)]
        for thread in racers:
            thread.start()
        for thread in racers:
            thread.join()
        self.assertEqual(racy.claim_inserts, 2)
        self.assertEqual(racy_receiver.accepted, 2)

    def test_crash_after_send_retries_the_gate_once(self) -> None:
        store, clock, receiver, _upstream, downstream, worker = _stack()
        body = helpers.standard_body()
        self.assertEqual(receiver.handle(helpers.signed_request(body, timestamp=clock.time())).status, 200)
        worker.crash_after_send = True
        with self.assertRaises(SimulatedCrash):
            worker.drain()
        self.assertEqual(downstream.attempts, 1)
        self.assertEqual(len(downstream.applied), 1)
        self.assertEqual(store.outbox["msg_quay_0001"]["status"], "pending")
        worker.drain()
        self.assertEqual(downstream.attempts, 2)
        self.assertEqual(len(downstream.applied), 1)
        self.assertEqual(store.inbox["msg_quay_0001"].status, "done")
        self.assertEqual(store.memo_misses, 1)
        self.assertEqual(worker.apply("msg_quay_0001")["object_id"], "rel_1001")
        self.assertEqual(store.memo_hits, 1)
        self.assertEqual(store.memo_misses, 1)

    def test_ack_before_commit_stops_the_publisher_with_an_empty_inbox(self) -> None:
        from quay_inbox.publisher import Publisher
        from quay_inbox.ssrf import ScriptedResolver

        store, clock, receiver, _upstream, _downstream, _worker = _stack(ack_before_commit=True)
        body = helpers.standard_body()
        publisher = Publisher.register(
            "https://hooks.quay.example/hooks/quay",
            ScriptedResolver({"hooks.quay.example": ["203.0.113.10"]}),
            clock=clock,
            rng=__import__("random").Random(1),
            transport=receiver.handle,
            profile="standard",
            secrets=[helpers.ROUTE_A],
        )
        publisher.enqueue("msg_quay_0001", body)
        publisher.pump()
        self.assertEqual(store.inbox, {})
        self.assertFalse(publisher.rows["msg_quay_0001"].automatic_open)
        self.assertEqual(store.memo_misses, 0)

    def test_prune_keeps_thirty_days_and_never_drops_unfinished_rows(self) -> None:
        store, clock, receiver, _upstream, _downstream, worker = _stack()
        body = helpers.standard_body()
        self.assertEqual(receiver.handle(helpers.signed_request(body, timestamp=clock.time())).status, 200)
        worker.drain()
        queued = helpers.signed_request(body, timestamp=clock.time(), event_id="msg_queued")
        self.assertEqual(receiver.handle(queued).status, 200)
        accepted_at = store.inbox["msg_quay_0001"].accepted_at
        self.assertEqual(store.prune(accepted_at + INBOX_RETENTION_SECONDS), [])
        self.assertIn("msg_quay_0001", store.inbox)
        self.assertEqual(store.prune(accepted_at + INBOX_RETENTION_SECONDS + 1), ["msg_quay_0001"])
        self.assertNotIn("msg_quay_0001", store.inbox)
        self.assertNotIn((0, ("msg_quay_0001", "apply")), store.memos)
        self.assertNotIn("msg_quay_0001", store.outbox)
        self.assertEqual(store.inbox["msg_queued"].status, "queued")

    def test_snapshot_restores_the_inbox_and_not_the_freshness_cache(self) -> None:
        store, clock, receiver, _upstream, _downstream, worker = _stack()
        body = helpers.standard_body()
        self.assertEqual(receiver.handle(helpers.signed_request(body, timestamp=clock.time())).status, 200)
        worker.drain()
        restored = helpers.Store()
        restored.restore(store.snapshot())
        self.assertEqual(restored.memo_misses, 1)
        self.assertEqual(restored.fresh, {})
        self.assertIn("msg_quay_0001", restored.inbox)
        again = Receiver(restored, clock, [helpers.standard_route()])
        clock.advance(10)
        response = again.handle(helpers.signed_request(body, timestamp=clock.time()))
        self.assertEqual(response.status, 200)
        self.assertEqual(restored.claim_duplicates, 1)
        self.assertEqual(restored.memo_misses, 1)


class StripeOrderTests(unittest.TestCase):
    def _receiver(self, clock, store):
        route = Route("stripe", "/hooks/stripe", "stripe", stripe_secrets=(DASHBOARD_SECRET,))
        return Receiver(store, clock, [route])

    def test_equal_created_refetches_and_does_not_drop_a_later_id(self) -> None:
        store, clock = helpers.make_store_clock()
        receiver = self._receiver(clock, store)
        current = {"berth": "Q7", "id": "rel_1001", "object": "release", "status": "settled"}
        upstream = Upstream({"rel_1001": current})
        worker = Worker(store, upstream, Downstream())
        created = 1_700_000_000
        settled = helpers.stripe_body(
            "evt_settled",
            "release.settled",
            {"berth": "OLD", "id": "rel_1001", "object": "release", "status": "settled"},
            created,
        )
        opened = helpers.stripe_body(
            "evt_opened",
            "release.opened",
            {"berth": "Q1", "id": "rel_1001", "object": "release", "status": "opened"},
            created,
        )
        self.assertEqual(
            receiver.handle(helpers.stripe_request(settled, DASHBOARD_SECRET, clock.time())).status,
            200,
        )
        self.assertEqual(
            receiver.handle(helpers.stripe_request(opened, DASHBOARD_SECRET, clock.time())).status,
            200,
        )
        worker.drain()
        self.assertEqual(worker.application_order, ["evt_settled", "evt_opened"])
        self.assertEqual(store.releases["rel_1001"]["status"], "settled")
        self.assertEqual(store.releases["rel_1001"]["berth"], "Q7")
        self.assertGreaterEqual(store.fetches, 1)
        self.assertIn("evt_opened", store.inbox)
        self.assertIn("evt_settled", store.inbox)

        granted = helpers.stripe_body(
            "evt_pair_1",
            "release.granted",
            {"berth": "Q3", "id": "rel_9", "object": "release", "status": "granted"},
            created + 5,
        )
        again = helpers.stripe_body(
            "evt_pair_2",
            "release.granted",
            {"berth": "Q3", "id": "rel_9", "object": "release", "status": "granted"},
            created + 6,
        )
        upstream.objects["rel_9"] = {
            "berth": "Q3",
            "id": "rel_9",
            "object": "release",
            "status": "granted",
        }
        self.assertEqual(receiver.handle(helpers.stripe_request(granted, DASHBOARD_SECRET, clock.time())).status, 200)
        self.assertEqual(receiver.handle(helpers.stripe_request(again, DASHBOARD_SECRET, clock.time())).status, 200)
        before = store.release_changes
        worker.drain()
        self.assertEqual(store.release_changes, before + 1)
        self.assertIn("evt_pair_1", store.inbox)
        self.assertIn("evt_pair_2", store.inbox)
        upstream.objects["rel_9"]["status"] = "held"
        upstream.objects["rel_9"]["berth"] = "Q8"
        later = helpers.stripe_body(
            "evt_pair_3",
            "release.granted",
            {"berth": "Q3", "id": "rel_9", "object": "release", "status": "granted"},
            created + 7,
        )
        self.assertEqual(receiver.handle(helpers.stripe_request(later, DASHBOARD_SECRET, clock.time())).status, 200)
        worker.drain()
        self.assertEqual(store.releases["rel_9"]["status"], "held")
        self.assertIn("evt_pair_3", store.inbox)

    def test_thin_event_uses_the_fetched_object(self) -> None:
        store, clock = helpers.make_store_clock()
        receiver = self._receiver(clock, store)
        upstream = Upstream(
            {
                "rel_thin": {
                    "berth": "Q5",
                    "id": "rel_thin",
                    "object": "release",
                    "status": "granted",
                }
            }
        )
        worker = Worker(store, upstream, Downstream())
        body = helpers.stripe_body(
            "evt_thin",
            "release.granted",
            {"id": "rel_thin", "object": "release"},
            1_700_000_000,
        )
        self.assertEqual(receiver.handle(helpers.stripe_request(body, DASHBOARD_SECRET, clock.time())).status, 200)
        worker.drain()
        self.assertEqual(store.fetches, 1)
        self.assertEqual(store.releases["rel_thin"]["status"], "granted")
        self.assertEqual(store.releases["rel_thin"]["berth"], "Q5")
        self.assertNotEqual(store.releases["rel_thin"], {"id": "rel_thin"})

    def test_schema_follows_the_event_version_not_the_process_default(self) -> None:
        self.assertEqual(CURRENT_API_VERSION, "2026-01-01")
        snapshot = {"berth": "Q3", "id": "rel_1001", "status": "granted"}
        self.assertEqual(validate_snapshot(snapshot, "2024-09-01", thin=False)["id"], "rel_1001")
        with self.assertRaises(SchemaError):
            validate_snapshot(snapshot, "2026-01-01", thin=False)
        store, clock, receiver, _upstream, _downstream, worker = _stack()
        body = helpers.standard_body(api_version="2024-09-01")
        self.assertEqual(receiver.handle(helpers.signed_request(body, timestamp=clock.time())).status, 200)
        worker.drain()
        self.assertEqual(store.releases["rel_1001"]["status"], "granted")
        newer = helpers.standard_body(api_version="2026-01-01", yard_code="Y1")
        response = receiver.handle(
            helpers.signed_request(newer, timestamp=clock.time(), event_id="msg_new")
        )
        self.assertEqual(response.status, 200)

    def test_all_events_subscription_skips_selection_required_types(self) -> None:
        events = [
            {"type": "release.granted", "id": "evt_1"},
            {"type": "release.audit_export", "id": "evt_2"},
            {"type": "release.container.moved", "id": "evt_3"},
        ]
        visible = filter_subscription(events, None)
        self.assertEqual([item["type"] for item in visible], ["release.granted", "release.container.moved"])
        selected = filter_subscription(events, {"release.audit_export"})
        self.assertEqual([item["id"] for item in selected], ["evt_2"])


if __name__ == "__main__":
    unittest.main()
