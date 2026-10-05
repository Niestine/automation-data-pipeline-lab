"""Publisher schedule, SSRF pin, cursor reconciliation, and idempotent POST."""

from __future__ import annotations

import json
import random
import tempfile
import threading
import unittest
from pathlib import Path

import helpers
from quay_inbox.catalog import Checkpoint, EventCatalog, Reconciler
from quay_inbox.errors import ApiUsageError, GapError, SsrfError
from quay_inbox.horizons import LIST_RETENTION_SECONDS, SCHEDULE_SECONDS, SPEC_TABLE_HORIZON
from quay_inbox.httpmsg import HttpResponse
from quay_inbox.idempotency import IdempotencyResource, parse_idempotency_key
from quay_inbox.publisher import Publisher
from quay_inbox.receiver import Receiver
from quay_inbox.schedule import bounded_slot_delay, full_jitter_delay, no_jitter_delay
from quay_inbox.secrets import DASHBOARD_SECRET
from quay_inbox.ssrf import ScriptedResolver, is_denied_address, pin_https
from quay_inbox.worker import Downstream, Upstream, Worker


class ScheduleTests(unittest.TestCase):
    def test_table_and_jitter_formulas(self) -> None:
        self.assertEqual(
            SCHEDULE_SECONDS,
            (0, 5, 300, 1800, 7200, 18000, 36000, 50400, 72000, 86400),
        )
        self.assertEqual(sum(SCHEDULE_SECONDS), SPEC_TABLE_HORIZON)
        rng = random.Random(0)
        delay = bounded_slot_delay(86400, rng)
        self.assertGreaterEqual(delay, 86400 - 86400 // 5)
        self.assertLessEqual(delay, 86400)
        self.assertGreaterEqual(delay, 1)
        self.assertEqual(bounded_slot_delay(0, random.Random(0)), 0)

        left = random.Random(12345)
        right = random.Random(12345)
        other = random.Random(99)
        got = [full_jitter_delay(attempt, left, base=8, cap=64) for attempt in range(5)]
        expected = []
        for attempt in range(5):
            ceiling = min(64, 8 * (2**attempt))
            expected.append(right.randrange(0, ceiling + 1))
        self.assertEqual(got, expected)
        other_seq = [full_jitter_delay(attempt, other, base=8, cap=64) for attempt in range(5)]
        self.assertNotEqual(got, other_seq)
        plain_a = [no_jitter_delay(attempt, base=8, cap=64) for attempt in range(5)]
        plain_b = [no_jitter_delay(attempt, base=8, cap=64) for attempt in range(5)]
        self.assertEqual(plain_a, plain_b)

    def test_retry_after_replaces_jitter_and_manual_replay_leaves_the_schedule(self) -> None:
        store, clock = helpers.make_store_clock()
        route = helpers.Route("stripe", "/hooks/stripe", "stripe", stripe_secrets=(DASHBOARD_SECRET,))
        receiver = Receiver(store, clock, [route])
        worker = Worker(store, Upstream({}), Downstream())
        calls = {"n": 0}

        def transport(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return HttpResponse(429, b"{}", {"retry-after": "17"})
            return receiver.handle(request)

        publisher = Publisher.register(
            "https://hooks.quay.example/hooks/stripe",
            ScriptedResolver({"hooks.quay.example": ["203.0.113.10"]}),
            clock=clock,
            rng=random.Random(3),
            transport=transport,
            profile="stripe",
            stripe_secrets=[DASHBOARD_SECRET],
        )
        body = helpers.stripe_body(
            "evt_1001",
            "release.granted",
            {"berth": "Q3", "id": "rel_1001", "object": "release", "status": "granted"},
            clock.time(),
        )
        publisher.enqueue("evt_1001", body)
        publisher.pump()
        row = publisher.rows["evt_1001"]
        self.assertEqual(row.delays, [17])
        self.assertTrue(row.automatic_open)
        saved_next = row.next_at
        publisher.manual_replay(["evt_1001"])
        self.assertEqual(row.next_at, saved_next)
        self.assertTrue(row.automatic_open)
        worker.drain()
        self.assertEqual(store.memo_misses, 1)
        clock.advance(17)
        publisher.pump()
        worker.drain()
        self.assertEqual(store.memo_misses, 1)
        self.assertFalse(row.automatic_open)
        self.assertGreaterEqual(calls["n"], 3)

    def test_sustained_503_spends_the_jitter_budget_then_walks_the_table(self) -> None:
        clock = helpers.ManualClock()

        def overloaded(_request):
            return HttpResponse(503, b"{}", {})

        publisher = Publisher.register(
            "https://hooks.quay.example/hooks/quay",
            ScriptedResolver({"hooks.quay.example": ["203.0.113.10"]}),
            clock=clock,
            rng=random.Random(5),
            transport=overloaded,
            profile="standard",
            secrets=[helpers.ROUTE_A],
            jitter_base=8,
            jitter_cap=64,
            max_throttle_retries=3,
        )
        publisher.enqueue("msg_overload", helpers.standard_body())
        row = publisher.rows["msg_overload"]
        started = clock.time()
        for _ in range(100):
            if not row.automatic_open:
                break
            publisher.pump()
            if row.automatic_open:
                clock.advance(row.next_at - clock.time())
        self.assertFalse(row.automatic_open, "retry schedule never ended")
        expected_rng = random.Random(5)
        jitter = [full_jitter_delay(attempt, expected_rng, base=8, cap=64) for attempt in range(3)]
        self.assertEqual(row.delays[:3], jitter)
        self.assertEqual(len(row.delays), 3 + len(SCHEDULE_SECONDS) - 1)
        self.assertTrue(row.exhausted)
        self.assertEqual(len(publisher.connections), 3 + len(SCHEDULE_SECONDS))
        # The table still spans most of 75:35:05; a 503 does not collapse it.
        self.assertGreaterEqual(clock.time() - started, SPEC_TABLE_HORIZON * 4 // 5)
        self.assertLessEqual(clock.time() - started, SPEC_TABLE_HORIZON + 3 * 64)

    def test_admin_replay_reaches_the_publisher_and_the_receiver_dedupes(self) -> None:
        store, clock = helpers.make_store_clock()
        worker = Worker(store, Upstream({}), Downstream())
        holder: dict[str, Publisher] = {}
        receiver = Receiver(
            store,
            clock,
            [helpers.standard_route()],
            admin_replay=lambda ids: holder["publisher"].manual_replay(ids),
        )
        publisher = Publisher.register(
            "https://hooks.quay.example/hooks/quay",
            ScriptedResolver({"hooks.quay.example": ["203.0.113.10"]}),
            clock=clock,
            rng=random.Random(2),
            transport=receiver.handle,
            profile="standard",
            secrets=[helpers.ROUTE_A],
        )
        holder["publisher"] = publisher
        publisher.enqueue("msg_quay_0001", helpers.standard_body())
        publisher.pump()
        worker.drain()
        self.assertEqual(store.memo_misses, 1)
        token = {"x-csrf-token": "csrf-lab-token"}
        unknown = receiver.handle(
            helpers.HttpRequest("POST", "/admin/replay", "ops.quay.example", token, b'{"event_ids":["msg_nope"]}')
        )
        self.assertEqual(unknown.status, 404)
        self.assertEqual(len(publisher.connections), 1)
        clock.advance(60)
        replay = receiver.handle(
            helpers.HttpRequest("POST", "/admin/replay", "ops.quay.example", token, b'{"event_ids":["msg_quay_0001"]}')
        )
        self.assertEqual(replay.status, 202)
        self.assertEqual(len(publisher.connections), 2)
        worker.drain()
        self.assertEqual(store.memo_misses, 1)
        self.assertEqual(receiver.duplicates, 1)
        self.assertEqual(store.claim_inserts, 1)

    def test_redirect_is_not_followed_and_410_disables(self) -> None:
        clock = helpers.ManualClock()
        seen: list[str] = []

        def transport(request):
            seen.append(request.peer)
            return HttpResponse(302, b"{}", {"location": "https://127.0.0.1/hooks"})

        publisher = Publisher.register(
            "https://hooks.quay.example/hooks/quay",
            ScriptedResolver({"hooks.quay.example": ["203.0.113.10"]}),
            clock=clock,
            rng=random.Random(1),
            transport=transport,
            profile="standard",
            secrets=[helpers.ROUTE_A],
        )
        publisher.enqueue("msg_1", helpers.standard_body())
        publisher.pump()
        self.assertEqual(publisher.locations_ignored, ["https://127.0.0.1/hooks"])
        self.assertEqual(seen, ["203.0.113.10"])
        clock.advance(5)
        publisher.pump()
        self.assertTrue(all(peer == "203.0.113.10" for peer in seen))

        def gone(_request):
            return HttpResponse(410, b"{}", {})

        stopped = Publisher.register(
            "https://hooks.quay.example/hooks/quay",
            ScriptedResolver({"hooks.quay.example": ["203.0.113.10"]}),
            clock=clock,
            rng=random.Random(1),
            transport=gone,
            profile="standard",
            secrets=[helpers.ROUTE_A],
        )
        stopped.enqueue("msg_2", helpers.standard_body())
        stopped.pump()
        self.assertTrue(stopped.disabled)
        before = len(stopped.connections)
        clock.advance(10)
        stopped.pump()
        self.assertEqual(len(stopped.connections), before)


class SsrfTests(unittest.TestCase):
    def test_pin_rejects_non_public_targets_and_does_not_resolve_twice(self) -> None:
        self.assertTrue(is_denied_address("127.0.0.1"))
        self.assertTrue(is_denied_address("10.1.2.3"))
        self.assertTrue(is_denied_address("192.168.0.8"))
        self.assertTrue(is_denied_address("169.254.169.254"))
        self.assertTrue(is_denied_address("fd00::1"))
        self.assertTrue(is_denied_address("::ffff:127.0.0.1"))
        self.assertTrue(is_denied_address("240.0.0.1"))
        self.assertFalse(is_denied_address("203.0.113.10"))
        with self.assertRaises(SsrfError):
            pin_https("https://localhost./hooks", ScriptedResolver({"localhost": ["203.0.113.10"]}))
        with self.assertRaises(SsrfError):
            pin_https("http://hooks.quay.example/hooks", ScriptedResolver({}))
        with self.assertRaises(SsrfError):
            pin_https("https://127.0.0.1/hooks", ScriptedResolver({}))
        with self.assertRaises(SsrfError):
            pin_https("https://169.254.169.254/latest", ScriptedResolver({}))
        with self.assertRaises(SsrfError):
            pin_https("https://metadata.google.internal/", ScriptedResolver({}))
        private = ScriptedResolver({"hooks.quay.example": ["192.168.0.8"]})
        with self.assertRaises(SsrfError):
            pin_https("https://hooks.quay.example/hooks/quay", private)

        class RebindingResolver(ScriptedResolver):
            def resolve(self, host: str) -> list[str]:
                self.calls.append(host.lower())
                if len(self.calls) == 1:
                    return ["203.0.113.10"]
                return ["127.0.0.1"]

        resolver = RebindingResolver({})
        target = pin_https("https://hooks.quay.example/hooks/quay", resolver)
        self.assertEqual(target.address, "203.0.113.10")
        self.assertEqual(target.host, "hooks.quay.example")
        self.assertEqual(target.sni, "hooks.quay.example")
        self.assertEqual(len(resolver.calls), 1)
        with self.assertRaises(SsrfError):
            pin_https("https://hooks.quay.example/hooks/quay", resolver)


class ReconcileTests(unittest.TestCase):
    def _notice(self, event_id: str, object_id: str, status: str = "granted"):
        from quay_inbox.lab import notice_for

        return notice_for(
            {
                "event_id": event_id,
                "type": "release.granted",
                "api_version": "2024-09-01",
                "created": 1_700_000_000,
                "data": {"berth": "Q3", "id": object_id, "status": status},
            }
        )

    def test_cursor_walk_survives_a_head_insertion(self) -> None:
        clock = helpers.ManualClock(1_700_000_100)
        events = []
        for number in range(1, 8):
            event_id = f"evt_{number:04d}"
            events.append(
                {
                    "id": event_id,
                    "created": 1_700_000_000 + number,
                    "type": "release.granted",
                    "delivery_success": False,
                    "notice": self._notice(event_id, f"rel_{number}"),
                }
            )
        catalog = EventCatalog(events, clock)
        store, _clock = helpers.make_store_clock(clock.time())
        receiver = Receiver(store, clock, [helpers.standard_route()])
        reconciler = Reconciler(catalog, receiver, Checkpoint(), limit=3, idempotency=None)

        def insert() -> None:
            catalog.insert(
                {
                    "id": "evt_0008",
                    "created": 1_700_000_050,
                    "type": "release.granted",
                    "delivery_success": False,
                    "notice": self._notice("evt_0008", "rel_8"),
                }
            )

        collected = reconciler.run(after_first_page=insert)
        self.assertEqual(sorted(collected), [f"evt_{number:04d}" for number in range(1, 9)])
        self.assertEqual(len(collected), len(set(collected)))

        items = [f"evt_{number:04d}" for number in range(7, 0, -1)]
        offset = 0
        offset_got: list[str] = []
        inserted = False
        while True:
            page = items[offset : offset + 3]
            if not page:
                break
            offset_got.extend(page)
            offset += 3
            if not inserted:
                items.insert(0, "evt_0008")
                inserted = True
            if len(page) < 3:
                break
        self.assertGreater(offset_got.count("evt_0005"), 1)

    def test_push_and_reconcile_apply_once_and_old_checkpoint_is_a_gap(self) -> None:
        store, clock = helpers.make_store_clock()
        receiver = Receiver(store, clock, [helpers.standard_route()])
        worker = Worker(store, Upstream({}), Downstream())
        body = helpers.standard_body(object_id="rel_1001")
        self.assertEqual(receiver.handle(helpers.signed_request(body, timestamp=clock.time())).status, 200)
        worker.drain()
        notice = self._notice("msg_quay_0001", "rel_1001")
        catalog = EventCatalog(
            [
                {
                    "id": "msg_quay_0001",
                    "created": clock.time(),
                    "type": "release.granted",
                    "delivery_success": False,
                    "notice": notice,
                }
            ],
            clock,
        )
        ran = {"n": 0}

        def handler(method, path, body, client_id):
            ran["n"] += 1
            return 201, body

        resource = IdempotencyResource(clock, handler, ttl=100)
        reconciler = Reconciler(catalog, receiver, Checkpoint(), idempotency=resource, limit=10)
        reconciler.run()
        reconciler.run()
        worker.drain()
        self.assertEqual(store.memo_misses, 1)
        self.assertEqual(ran["n"], 1)

        # The second run's ack is the stored 201 replayed, not a second call.
        self.assertEqual(reconciler.acked, ["msg_quay_0001", "msg_quay_0001"])

        failing = IdempotencyResource(clock, lambda method, path, body, client_id: (503, b"{}"), ttl=100)
        not_acked = Reconciler(catalog, receiver, Checkpoint(), idempotency=failing, limit=10)
        not_acked.run()
        self.assertEqual(not_acked.acked, [])

        stale = Checkpoint(watermark_created=clock.time() - (31 * 24 * 3600))
        blocked = Reconciler(catalog, receiver, stale, limit=10)
        with self.assertRaises(GapError):
            blocked.run()
        self.assertEqual(store.memo_misses, 1)
        self.assertGreater(31 * 24 * 3600, LIST_RETENTION_SECONDS)

        with self.assertRaises(ApiUsageError):
            catalog.list_events(type="release.granted", types=["release.granted"])
        with self.assertRaises(ApiUsageError):
            catalog.list_events(types=[f"type_{index}" for index in range(21)])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            reconciler.checkpoint.save(path)
            loaded = Checkpoint.load(path)
            self.assertEqual(loaded.watermark_created, reconciler.checkpoint.watermark_created)


    def test_watermark_tracks_the_walk_not_the_oldest_listed_event(self) -> None:
        clock = helpers.ManualClock(1_700_000_100 + 29 * 24 * 3600)
        now = clock.time()
        events = [
            {
                "id": "evt_old",
                "created": now - 29 * 24 * 3600,
                "type": "release.granted",
                "delivery_success": False,
                "notice": self._notice("evt_old", "rel_old"),
            },
            {
                "id": "evt_new",
                "created": now,
                "type": "release.granted",
                "delivery_success": False,
                "notice": self._notice("evt_new", "rel_new"),
            },
        ]
        catalog = EventCatalog(events, clock)
        store, _clock = helpers.make_store_clock(now)
        receiver = Receiver(store, clock, [helpers.standard_route()])
        checkpoint = Checkpoint()
        reconciler = Reconciler(catalog, receiver, checkpoint, limit=10)
        reconciler.run()
        self.assertEqual(checkpoint.watermark_created, now)
        self.assertEqual(checkpoint.last_id, "evt_old")
        # Two days later evt_old is 31 days old, but the last walk was two
        # days ago, so nothing could have aged out unseen.
        clock.advance(2 * 24 * 3600)
        self.assertEqual(reconciler.run(), ["evt_new"])
        self.assertEqual(checkpoint.watermark_created, clock.time())
        clock.advance(LIST_RETENTION_SECONDS + 1)
        with self.assertRaises(GapError):
            reconciler.run()

        dry_store, _ = helpers.make_store_clock(clock.time())
        dry = Reconciler(
            catalog,
            Receiver(dry_store, clock, [helpers.standard_route()], dry_run=True),
            Checkpoint(),
            limit=10,
        )
        dry.run()
        self.assertIsNone(dry.checkpoint.watermark_created)


class IdempotencyTests(unittest.TestCase):
    def test_draft_error_model_and_client_isolation(self) -> None:
        clock = helpers.ManualClock(1_000)
        calls = {"n": 0}

        def handler(method, path, body, client_id):
            calls["n"] += 1
            if b'"boom":true' in body:
                return 500, b'{"error":"stored"}'
            return 201, json.dumps({"owner": client_id, "method": method}).encode("utf-8")

        resource = IdempotencyResource(clock, handler, ttl=100)
        missing = resource.handle(
            client_id="A",
            method="POST",
            path="/v1/releases/ack",
            header=None,
            body=b"{}",
        )
        self.assertEqual(missing.status, 400)
        self.assertEqual(missing.headers["content-type"], "application/problem+json")
        unquoted = resource.handle(
            client_id="A",
            method="POST",
            path="/v1/releases/ack",
            header="8e03978e-40d5-43e8-bc93-6894a57f9324",
            body=b"{}",
        )
        self.assertEqual(unquoted.status, 400)
        self.assertIsNone(parse_idempotency_key("8e03978e-40d5-43e8-bc93-6894a57f9324"))
        self.assertEqual(resource.rows, {})

        body = b'{"event_id":"evt_1"}'
        key = '"evt_1"'
        first = resource.handle(client_id="A", method="POST", path="/v1/releases/ack", header=key, body=body)
        second = resource.handle(client_id="A", method="POST", path="/v1/releases/ack", header=key, body=body)
        self.assertEqual(first.status, 201)
        self.assertEqual(second.body, first.body)
        self.assertEqual(calls["n"], 1)
        other = resource.handle(
            client_id="A",
            method="POST",
            path="/v1/releases/ack",
            header=key,
            body=b'{"event_id":"evt_1","extra":1}',
        )
        self.assertEqual(other.status, 422)
        self.assertEqual(calls["n"], 1)
        patch = resource.handle(client_id="A", method="PATCH", path="/v1/releases/ack", header=key, body=body)
        self.assertEqual(patch.status, 201)
        self.assertEqual(calls["n"], 2)
        foreign = resource.handle(client_id="B", method="POST", path="/v1/releases/ack", header=key, body=body)
        self.assertEqual(json.loads(foreign.body)["owner"], "B")
        self.assertNotEqual(foreign.body, first.body)

        boom = b'{"boom":true}'
        failed = resource.handle(
            client_id="A",
            method="POST",
            path="/v1/releases/ack",
            header='"boom-1"',
            body=boom,
        )
        replay = resource.handle(
            client_id="A",
            method="POST",
            path="/v1/releases/ack",
            header='"boom-1"',
            body=boom,
        )
        self.assertEqual(failed.status, 500)
        self.assertEqual(replay.body, failed.body)
        runs_after_stored_error = calls["n"]
        resource.handle(
            client_id="A",
            method="POST",
            path="/v1/releases/ack",
            header='"boom-1"',
            body=boom,
        )
        self.assertEqual(calls["n"], runs_after_stored_error)

        clock.advance(99)
        early = resource.handle(client_id="A", method="POST", path="/v1/releases/ack", header=key, body=body)
        self.assertEqual(early.body, first.body)
        clock.advance(1)
        renewed = resource.handle(client_id="A", method="POST", path="/v1/releases/ack", header=key, body=body)
        self.assertEqual(renewed.status, 201)
        self.assertGreater(calls["n"], runs_after_stored_error)

        started = threading.Event()
        release = threading.Event()

        def blocking(method, path, body, client_id):
            started.set()
            release.wait(5)
            return 201, b'{"owner":"A"}'

        blocking_resource = IdempotencyResource(clock, blocking, ttl=100)
        result: dict[str, HttpResponse] = {}

        def first_call() -> None:
            result["first"] = blocking_resource.handle(
                client_id="A",
                method="POST",
                path="/v1/hold",
                header='"hold-1"',
                body=b"{}",
            )

        thread = threading.Thread(target=first_call)
        thread.start()
        self.assertTrue(started.wait(5))
        conflict = blocking_resource.handle(
            client_id="A",
            method="POST",
            path="/v1/hold",
            header='"hold-1"',
            body=b"{}",
        )
        self.assertEqual(conflict.status, 409)
        release.set()
        thread.join(5)
        self.assertEqual(result["first"].status, 201)


if __name__ == "__main__":
    unittest.main()
