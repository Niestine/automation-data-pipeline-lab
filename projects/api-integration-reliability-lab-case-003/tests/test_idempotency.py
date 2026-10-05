"""Three-state idempotency record, separate from If-Match."""

from __future__ import annotations

import json
import threading
import unittest

import helpers
from plot_allotment.client import ExportClient
PARENT = helpers.PARENT
KEY_A = "11111111-1111-4111-8111-111111111111"
KEY_B = "22222222-2222-4222-8222-222222222222"
KEY_C = "33333333-3333-4333-8333-333333333333"


class IdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lab = helpers.Lab(seed=2)
        self.addCleanup(self.lab.close)

    def test_lost_response_replays_one_row(self) -> None:
        body = helpers.lot("lot-01", 1)
        self.lab.service.drop_response_once = True
        response = self.lab.client.upsert(PARENT, body, KEY_A)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.header("x-idempotency-replay"), "stored")
        self.assertEqual(self.lab.store.ledger_count(PARENT), 1)
        self.assertEqual(len(self.lab.store.executions), 1)
        self.assertTrue(self.lab.journal.has("timeout_after_commit", "replay_stored_body"))
        self.assertNotIn("request_id", self.lab.store.ledger_columns())

    def test_different_payload_is_422_and_other_caller_is_isolated(self) -> None:
        body = helpers.lot("lot-01", 1)
        first = self.lab.client.upsert(PARENT, body, KEY_A)
        self.assertEqual(first.status, 200)
        before = self.lab.store.ledger_get(PARENT, "lot-01")
        calls = self.lab.client.calls
        mismatch = self.lab.client.upsert(PARENT, helpers.lot("lot-01", 1, note="revised"), KEY_A)
        self.assertEqual(mismatch.status, 422)
        self.assertEqual(self.lab.client.calls, calls + 1)
        self.assertTrue(self.lab.journal.has("fingerprint_422", "stop_payload_mismatch"))
        self.assertTrue(self.lab.journal.has("client_status_422", "do_not_retry"))
        self.assertEqual(self.lab.store.ledger_get(PARENT, "lot-01")["note"], before["note"])
        self.assertEqual(self.lab.store.ledger_get(PARENT, "lot-01")["etag"], before["etag"])
        other = ExportClient(
            self.lab.service,
            "desk-b",
            sleeper=self.lab.sleeper,
            rng=self.lab.rng,
            journal=self.lab.journal,
        )
        second = other.upsert(PARENT, body, KEY_A)
        self.assertEqual(second.status, 200)
        self.assertNotEqual(second.body, first.body)
        self.assertEqual(len(self.lab.store.executions), 2)

    def test_in_progress_returns_409_and_that_409_is_not_stored(self) -> None:
        body = helpers.lot("lot-09", 9)
        gate = threading.Event()
        self.lab.service.pause_once = gate
        holder: list = []

        def call() -> None:
            holder.append(
                self.lab.service.handle(helpers.upsert_request(body, f'"{KEY_A}"'))
            )

        thread = threading.Thread(target=call)
        thread.start()
        for _ in range(100):
            if self.lab.store.idem_row(helpers.CALLER, KEY_A) is not None:
                break
            if not thread.is_alive():
                self.fail("upsert thread exited before publishing in_progress")
            thread.join(0.05)
        self.assertEqual(self.lab.store.idem_row(helpers.CALLER, KEY_A)["state"], "in_progress")
        conflict = self.lab.service.handle(helpers.upsert_request(body, f'"{KEY_A}"'))
        self.assertEqual(conflict.status, 409)
        self.assertIn(b"outstanding", conflict.body)
        gate.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder[0].status, 200)
        replay = self.lab.service.handle(helpers.upsert_request(body, f'"{KEY_A}"'))
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay.header("x-idempotency-replay"), "stored")
        self.assertEqual(replay.body, holder[0].body)
        stored = self.lab.store.idem_row(helpers.CALLER, KEY_A)
        self.assertEqual(stored["status"], 200)
        self.assertNotIn("409", stored["response_body"])
        self.assertEqual(self.lab.store.ledger_count(PARENT), 1)
        self.assertTrue(self.lab.journal.has("idempotency_409", "retry_without_changes"))

    def test_rolled_back_handler_releases_the_claim_and_the_retry_executes_once(self) -> None:
        body = helpers.lot("lot-05", 5)
        self.lab.service.fail_upsert_commit = True
        response = self.lab.client.upsert(PARENT, body, KEY_A)
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.header("x-idempotency-replay"))
        self.assertEqual(self.lab.client.calls, 2)
        self.assertTrue(self.lab.journal.has("handler_error", "release_claim"))
        self.assertTrue(self.lab.journal.has("client_status_500", "full_jitter_retry"))
        self.assertFalse(self.lab.journal.has("idempotency_409", "retry_without_changes"))
        self.assertEqual(len(self.lab.store.executions), 1)
        self.assertEqual(self.lab.store.ledger_count(PARENT), 1)
        self.assertEqual(self.lab.store.idem_row(helpers.CALLER, KEY_A)["state"], "completed")

    def test_expired_key_executes_again_and_pruned_body_returns_current(self) -> None:
        body = helpers.lot("lot-01", 1)
        first = self.lab.client.upsert(PARENT, body, KEY_A)
        self.assertEqual(first.status, 200)
        row = self.lab.store.idem_row(helpers.CALLER, KEY_A)
        self.lab.clock.advance(float(row["expires_at"]) - self.lab.clock.now() - 1)
        replay = self.lab.client.upsert(PARENT, body, KEY_A)
        self.assertEqual(replay.header("x-idempotency-replay"), "stored")
        self.assertEqual(len(self.lab.store.executions), 1)
        self.lab.clock.advance(2)
        again = self.lab.client.upsert(PARENT, body, KEY_A)
        self.assertEqual(again.status, 200)
        self.assertIsNone(again.header("x-idempotency-replay"))
        self.assertEqual(len(self.lab.store.executions), 2)

        self.lab.store.prune_idempotency_body(helpers.CALLER, KEY_A)
        updated = helpers.lot("lot-01", 1, note="revised", version=2)
        second = self.lab.client.upsert(PARENT, updated, KEY_B, if_match=again.header("etag"))
        self.assertEqual(second.status, 200)
        substituted = self.lab.client.upsert(PARENT, body, KEY_A)
        self.assertEqual(substituted.status, 200)
        self.assertEqual(substituted.header("x-idempotency-replay"), "current-resource")
        self.assertEqual(json.loads(substituted.body)["resource"]["note"], "revised")

    def test_missing_and_malformed_keys_do_not_read_the_store(self) -> None:
        body = helpers.lot("lot-01", 1)
        missing = self.lab.service.handle(helpers.upsert_request(body, None))
        self.assertEqual(missing.status, 400)
        self.assertEqual(self.lab.store.idempotency_lookups, 0)
        self.assertEqual(self.lab.store.ledger_count(), 0)
        malformed = self.lab.service.handle(helpers.upsert_request(body, "not-a-uuid"))
        self.assertEqual(malformed.status, 400)
        quoted = self.lab.service.handle(helpers.upsert_request(body, '"not-a-uuid"'))
        self.assertEqual(quoted.status, 400)
        self.assertEqual(self.lab.store.idempotency_lookups, 0)
        self.assertEqual(self.lab.store.ledger_count(), 0)

    def test_request_id_is_not_stored_and_if_match_is_ordered_after_replay(self) -> None:
        body = helpers.lot("lot-01", 1)
        body["request_id"] = "44444444-4444-4444-8444-444444444444"
        created = self.lab.client.upsert(PARENT, body, KEY_A)
        self.assertEqual(created.status, 200)
        resource = json.loads(created.body)["resource"]
        self.assertNotIn("request_id", resource)
        etag = created.header("etag")
        updated = helpers.lot("lot-01", 1, note="revised", version=2)
        changed = self.lab.client.upsert(PARENT, updated, KEY_B, if_match=etag)
        self.assertEqual(changed.status, 200)
        new_etag = changed.header("etag")
        self.assertNotEqual(new_etag, etag)
        executions = len(self.lab.store.executions)
        replay = self.lab.client.upsert(PARENT, updated, KEY_B, if_match=etag)
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay.header("x-idempotency-replay"), "stored")
        self.assertEqual(replay.body, changed.body)
        self.assertEqual(len(self.lab.store.executions), executions)
        self.assertEqual(self.lab.store.ledger_get(PARENT, "lot-01")["etag"], new_etag)
        blocked = self.lab.client.upsert(PARENT, updated, KEY_C, if_match=etag)
        self.assertEqual(blocked.status, 412)
        self.assertEqual(self.lab.store.ledger_get(PARENT, "lot-01")["etag"], new_etag)
        self.assertIsNone(self.lab.store.idem_row(helpers.CALLER, KEY_C))
        corrected = self.lab.client.upsert(PARENT, helpers.lot("lot-01", 4, note="hold", version=3), KEY_C, if_match=new_etag)
        self.assertEqual(corrected.status, 200)
        self.assertEqual(self.lab.store.ledger_get(PARENT, "lot-01")["note"], "hold")
        self.assertTrue(self.lab.journal.has("precondition_412", "stop_not_a_replay"))

    def test_same_key_after_412_can_retry_once_the_tag_matches(self) -> None:
        # Covered by the corrected retry above; this pins the deleted claim.
        created = self.lab.client.upsert(PARENT, helpers.lot("lot-04", 4), KEY_A)
        stale = self.lab.client.upsert(
            PARENT,
            helpers.lot("lot-04", 4, note="revised", version=2),
            KEY_B,
            if_match='"e-does-not-match"',
        )
        self.assertEqual(stale.status, 412)
        self.assertEqual(self.lab.store.ledger_get(PARENT, "lot-04")["etag"], created.header("etag"))
        self.assertIsNone(self.lab.store.idem_row(helpers.CALLER, KEY_B))

    def test_weak_if_match_is_rejected_before_the_idempotency_lookup(self) -> None:
        response = self.lab.service.handle(
            helpers.upsert_request(
                helpers.lot("lot-01", 1),
                f'"{KEY_A}"',
                if_match='W/"e-0001"',
            )
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.lab.store.idempotency_lookups, 0)
        self.assertIsNone(self.lab.store.idem_row(helpers.CALLER, KEY_A))
        self.assertEqual(self.lab.store.ledger_count(PARENT), 0)


if __name__ == "__main__":
    unittest.main()
