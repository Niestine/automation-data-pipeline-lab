"""Idempotent restock orders across refresh, conflict, and expiry."""

from __future__ import annotations

import json
import threading
import time

from helpers import LabCase, order_request, token_posts
from lotcycle.params import ACCESS_LIFETIME, IDEMPOTENCY_TTL
from lotcycle.schema import fingerprint
from lotcycle.tokens import b64url, b64url_decode


class OrderTest(LabCase):
    def test_same_key_and_body_replay_one_order(self) -> None:
        client = self.north()
        first = client.post_order({"qty": 2, "sku": "crate-ice"}, "restock-north-001")
        second = client.post_order({"qty": 2, "sku": "crate-ice"}, "restock-north-001")
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 201)
        self.assertEqual(second.body, first.body)
        self.assertEqual(second.headers["content-type"], first.headers["content-type"])
        self.assertEqual(self.lab.store.order_count(), 1)
        self.assertEqual(first.json()["order_id"], "ord-0001")
        self.assertEqual(first.json()["qty"], 2)

    def test_in_flight_key_returns_409_then_the_stored_body(self) -> None:
        client = self.north()
        gate = threading.Event()
        self.lab.resources["lot-ledger"].park_gate = gate
        request = order_request(
            client.access_token, {"qty": 2, "sku": "crate-ice"}, "park-key"
        )
        results: list = []

        def run() -> None:
            results.append(self.lab.resources["lot-ledger"].handle(request))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            row = None
            deadline = time.time() + 2
            while time.time() < deadline:
                row = self.lab.store.read_idempotency(
                    "handheld-north", "POST", "/lot-ledger/orders", "park-key"
                )
                if row is not None and row["state"] == "in_flight":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(row)
            self.assertEqual(row["state"], "in_flight")
            conflict = self.lab.resources["lot-ledger"].handle(request)
            self.assertEqual(conflict.status, 409)
            gate.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        finally:
            gate.set()
            thread.join(3)
        self.assertEqual(results[0].status, 201)
        replay = self.lab.resources["lot-ledger"].handle(request)
        self.assertEqual(replay.status, 201)
        self.assertEqual(replay.body, results[0].body)
        self.assertEqual(self.lab.store.order_count(), 1)

    def test_different_body_is_422_and_keeps_the_stored_row(self) -> None:
        client = self.north()
        first = client.post_order({"qty": 2, "sku": "crate-ice"}, "body-key")
        stored = self.lab.store.read_idempotency(
            "handheld-north", "POST", "/lot-ledger/orders", "body-key"
        )
        digest = stored["fingerprint"]
        second = client.post_order({"qty": 3, "sku": "crate-ice"}, "body-key")
        self.assertEqual(second.status, 422)
        self.assertEqual(second.json()["type"], "about:blank")
        kept = self.lab.store.read_idempotency(
            "handheld-north", "POST", "/lot-ledger/orders", "body-key"
        )
        self.assertEqual(kept["fingerprint"], digest)
        self.assertEqual(bytes(kept["response_body"]), first.body)
        replay = client.post_order({"qty": 2, "sku": "crate-ice"}, "body-key")
        self.assertEqual(replay.status, 201)
        self.assertEqual(replay.body, first.body)
        self.assertEqual(self.lab.store.order_count(), 1)

    def test_missing_and_unquoted_keys_write_nothing(self) -> None:
        token = self.north().access_token
        missing = self.lab.resources["lot-ledger"].handle(
            order_request(token, {"qty": 2, "sku": "crate-ice"}, None)
        )
        unquoted = self.lab.resources["lot-ledger"].handle(
            order_request(token, {"qty": 2, "sku": "crate-ice"}, "plain-key", quoted=False)
        )
        self.assertEqual(missing.status, 400)
        self.assertEqual(unquoted.status, 400)
        self.assertEqual(missing.json()["type"], "about:blank")
        self.assertEqual(self.lab.store.idempotency_count(), 0)
        self.assertEqual(self.lab.store.order_count(), 0)

    def test_schema_errors_write_nothing(self) -> None:
        token = self.north().access_token
        boolean_qty = self.lab.resources["lot-ledger"].handle(
            order_request(token, b'{"qty":true,"sku":"crate-ice"}', "bad-qty")
        )
        bad_sku = self.lab.resources["lot-ledger"].handle(
            order_request(token, {"qty": 1, "sku": "NOPE"}, "bad-sku")
        )
        self.assertEqual(boolean_qty.status, 400)
        self.assertEqual(bad_sku.status, 400)
        self.assertEqual(self.lab.store.idempotency_count(), 0)

    def test_unstocked_sku_replays_the_stored_404(self) -> None:
        client = self.north()
        first = client.post_order({"qty": 1, "sku": "missing-lot"}, "missing-key")
        second = client.post_order({"qty": 1, "sku": "missing-lot"}, "missing-key")
        self.assertEqual(first.status, 404)
        self.assertEqual(second.status, 404)
        self.assertEqual(second.body, first.body)
        self.assertEqual(second.headers["content-type"], "application/problem+json")
        self.assertEqual(self.lab.store.order_count(), 0)
        self.assertEqual(self.lab.store.idempotency_count(), 1)

    def test_same_key_string_is_per_client(self) -> None:
        self.north().post_order({"qty": 2, "sku": "crate-ice"}, "shared-key")
        kiosk = self.lab.clients["kiosk-public"]
        kiosk.post_order({"qty": 1, "sku": "pack-wrap"}, "shared-key")
        self.assertEqual(self.lab.store.order_count(), 2)
        self.assertEqual(self.lab.store.idempotency_count(), 2)
        self.assertEqual(self.lab.store.order_count("handheld-north"), 1)
        self.assertEqual(self.lab.store.order_count("kiosk-public"), 1)

    def test_key_expires_at_24_hours(self) -> None:
        client = self.north()
        first = client.post_order({"qty": 2, "sku": "crate-ice"}, "ttl-key")
        now = self.lab.clock.now()
        self.lab.store.con.execute(
            "UPDATE idempotency SET created_at = ? WHERE idem_key = ?",
            (now - (IDEMPOTENCY_TTL - 1), "ttl-key"),
        )
        replay = client.post_order({"qty": 2, "sku": "crate-ice"}, "ttl-key")
        self.assertEqual(replay.body, first.body)
        self.assertEqual(self.lab.store.order_count(), 1)
        self.lab.store.con.execute(
            "UPDATE idempotency SET created_at = ? WHERE idem_key = ?",
            (self.lab.clock.now() - IDEMPOTENCY_TTL, "ttl-key"),
        )
        again = client.post_order({"qty": 2, "sku": "crate-ice"}, "ttl-key")
        self.assertEqual(again.status, 201)
        self.assertNotEqual(again.body, first.body)
        self.assertEqual(self.lab.store.order_count(), 2)
        self.assertEqual(token_posts(self.lab), 0)

    def test_expired_access_token_refreshes_once_and_keeps_the_key(self) -> None:
        client = self.north()
        self.lab.clock.advance(ACCESS_LIFETIME)
        client.access_exp = self.lab.clock.now() + 30
        response = client.post_order({"qty": 2, "sku": "crate-ice"}, "after-401")
        self.assertEqual(response.status, 201)
        self.assertEqual(self.lab.store.order_count(), 1)
        self.assertEqual(token_posts(self.lab), 1)
        order_sends = [path for path in self.lab.transport.sent if path.endswith("/orders")]
        self.assertEqual(order_sends, ["/lot-ledger/orders", "/lot-ledger/orders"])
        self.assertIn("order_refresh_after_401", client.decisions)

    def test_rolled_back_order_retries_without_refresh(self) -> None:
        client = self.north()
        self.lab.resources["lot-ledger"].fail_order_commits = 1
        response = client.post_order({"qty": 2, "sku": "pack-gel"}, "rollback-key")
        self.assertEqual(response.status, 201)
        self.assertEqual(self.lab.store.order_count(), 1)
        self.assertEqual(token_posts(self.lab), 0)
        states = [
            row["state"]
            for row in self.lab.store.con.execute("SELECT state FROM idempotency").fetchall()
        ]
        self.assertEqual(states, ["completed"])
        self.assertIn("order_retry", client.decisions)

    def test_forced_rollback_undoes_the_insert_and_frees_the_key(self) -> None:
        ledger = self.lab.resources["lot-ledger"]
        ledger.fail_order_commits = 1
        request = order_request(
            self.north().access_token, {"qty": 2, "sku": "pack-gel"}, "rolled-key"
        )
        failed = ledger.handle(request)
        self.assertEqual(failed.status, 500)
        self.assertEqual(failed.headers["content-type"], "application/problem+json")
        # The order insert ran inside the aborted transaction and is gone.
        self.assertEqual(self.lab.store.order_count(), 0)
        self.assertEqual(self.lab.store.idempotency_count(), 0)
        self.assertIn("order_rollback", [row["event"] for row in self.lab.store.logs])
        retried = ledger.handle(request)
        self.assertEqual(retried.status, 201)
        self.assertEqual(retried.json()["order_id"], "ord-0001")
        self.assertEqual(self.lab.store.order_count(), 1)

    def test_dropped_order_response_retries_without_sleep_or_refresh(self) -> None:
        client = self.north()
        self.lab.transport.drop_after_send = 1
        stamp = self.lab.clock.now()
        response = client.post_order({"qty": 2, "sku": "crate-ice"}, "drop-order")
        self.assertEqual(response.status, 201)
        self.assertEqual(self.lab.clock.now(), stamp)
        self.assertEqual(self.lab.store.order_count(), 1)
        self.assertEqual(token_posts(self.lab), 0)
        self.assertIn("order_ResponseDropped", client.decisions)
        replay = client.post_order({"qty": 2, "sku": "crate-ice"}, "drop-order")
        self.assertEqual(replay.body, response.body)

    def test_connection_closed_on_the_order_retries_the_same_body(self) -> None:
        client = self.north()
        self.lab.transport.fail_before_send = 1
        stamp = self.lab.clock.now()
        response = client.post_order({"qty": 2, "sku": "crate-ice"}, "closed-order")
        self.assertEqual(response.status, 201)
        self.assertGreater(self.lab.clock.now(), stamp)
        self.assertEqual(token_posts(self.lab), 0)
        self.assertIn("order_ConnectionClosed", client.decisions)
        self.assertEqual(self.lab.store.order_count(), 1)

    def test_flipped_mac_is_401_before_the_idempotency_lookup(self) -> None:
        token = self.north().access_token
        raw_part, mac_part = token.split(".", 1)
        mac = bytearray(b64url_decode(mac_part))
        mac[0] ^= 0x01
        forged = raw_part + "." + b64url(bytes(mac))
        before = self.lab.store.idempotency_count()
        response = self.lab.resources["lot-ledger"].handle(
            order_request(forged, {"qty": 2, "sku": "crate-ice"}, "forged-key")
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(self.lab.store.idempotency_count(), before)

    def test_narrow_and_south_scopes_do_not_refresh(self) -> None:
        client = self.north()
        client.refresh("excursions.read")
        denied = client.post_order({"qty": 1, "sku": "crate-ice"}, "narrow-order")
        self.assertEqual(denied.status, 403)
        self.assertIn("insufficient_scope", denied.body.decode("utf-8"))
        self.assertEqual(self.lab.store.idempotency_count(), 0)
        self.assertNotIn("order_refresh_after_401", client.decisions)

        south = self.lab.clients["handheld-south"]
        south_denied = south.post_order({"qty": 1, "sku": "crate-ice"}, "south-order")
        self.assertEqual(south_denied.status, 403)
        self.assertEqual(self.lab.store.idempotency_count(), 0)
        self.assertEqual(token_posts(self.lab), 1)
        self.assertNotIn("order_refresh_after_401", south.decisions)

    def test_fingerprint_covers_method_path_and_raw_body(self) -> None:
        body = b'{"qty":2,"sku":"crate-ice"}'
        digest = fingerprint("POST", "/lot-ledger/orders", body)
        self.assertEqual(digest, fingerprint("POST", "/lot-ledger/orders", body))
        self.assertNotEqual(digest, fingerprint("POST", "/lot-ledger/orders", body + b" "))
        self.assertEqual(len(digest), 64)
        parsed = json.loads(body)
        self.assertEqual(parsed["sku"], "crate-ice")
