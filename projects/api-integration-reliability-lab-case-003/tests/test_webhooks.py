"""Raw-body signatures, ack-after-commit, and the shortened sender ladder."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import helpers
from plot_allotment.httpmsg import Request, Response
from plot_allotment.schema import (
    INBOX_RETENTION_SECONDS,
    LAB_WEBHOOK_SECRET,
    SPEC_LADDER_LAST_SECONDS,
    canonical_bytes,
)
from plot_allotment.webhooks import (
    SHORT_LADDER_SECONDS,
    SPEC_LADDER_SECONDS,
    Endpoint,
    WebhookSender,
    decode_whsec,
    encode_whsec,
    sign,
    sign_rotation,
)

PARENT = helpers.PARENT
ROOT = Path(__file__).resolve().parents[1]


def _payload() -> dict:
    return {
        "data": {
            "beds": 2,
            "holder_label": "holder-09",
            "note": "revised",
            "parent": PARENT,
            "plot_code": "N-09",
            "resource_id": "lot-09",
            "sort_key": 9,
            "source_version": 2,
        },
        "type": "allotment.revised",
    }


def _post(lab: helpers.Lab, raw: bytes, webhook_id: str, timestamp: str, signature: str) -> Response:
    return lab.service.handle(
        Request(
            method="POST",
            path="/webhooks/events",
            headers={
                "content-type": "application/json",
                "webhook-id": webhook_id,
                "webhook-signature": signature,
                "webhook-timestamp": timestamp,
            },
            body=raw,
        )
    )


class WebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lab = helpers.Lab(seed=5)
        self.addCleanup(self.lab.close)

    def test_whitespace_and_reserialization_fail_verification(self) -> None:
        raw = canonical_bytes(_payload())
        timestamp = str(int(self.lab.clock.now()))
        signature = sign("msg_lot_09", timestamp, raw, LAB_WEBHOOK_SECRET)
        mutated = raw.replace(b"{", b"{ ", 1)
        rejected = _post(self.lab, mutated, "msg_lot_09", timestamp, signature)
        self.assertEqual(rejected.status, 401)
        pretty = json.dumps(json.loads(raw.decode("utf-8")), indent=2).encode("utf-8")
        reserialized = _post(self.lab, pretty, "msg_lot_09", timestamp, signature)
        self.assertEqual(reserialized.status, 401)
        self.assertEqual(self.lab.store.inbox_ids(), [])

    def test_oversized_body_is_rejected_before_the_mac(self) -> None:
        raw = b"{" + (b"x" * (20 * 1024))
        response = _post(self.lab, raw, "msg_huge", str(int(self.lab.clock.now())), "v1,not-a-mac")
        self.assertEqual(response.status, 413)
        self.assertEqual(self.lab.store.inbox_ids(), [])
        self.assertFalse(self.lab.journal.has("webhook_signature", "reject"))

    def test_skew_dot_duplicate_and_commit_failure(self) -> None:
        raw = canonical_bytes(_payload())
        late = str(int(self.lab.clock.now()) + 301)
        signature = sign("msg_lot_09", late, raw, LAB_WEBHOOK_SECRET)
        skewed = _post(self.lab, raw, "msg_lot_09", late, signature)
        self.assertEqual(skewed.status, 401)
        dotted = _post(
            self.lab,
            raw,
            "msg.lot",
            str(int(self.lab.clock.now())),
            sign("msg.lot", str(int(self.lab.clock.now())), raw, LAB_WEBHOOK_SECRET),
        )
        self.assertEqual(dotted.status, 400)

        now = str(int(self.lab.clock.now()))
        good = sign("msg_lot_09", now, raw, LAB_WEBHOOK_SECRET)
        self.lab.service.fail_webhook_commit = True
        failed = _post(self.lab, raw, "msg_lot_09", now, good)
        self.assertEqual(failed.status, 500)
        self.assertEqual(self.lab.store.inbox_ids(), [])
        self.assertEqual(self.lab.store.ledger_count(), 0)
        self.assertTrue(self.lab.journal.has("webhook_commit", "return_5xx_no_inbox"))
        applied = _post(self.lab, raw, "msg_lot_09", now, good)
        self.assertEqual(applied.status, 204)
        duplicate = _post(self.lab, raw, "msg_lot_09", now, good)
        self.assertEqual(duplicate.status, 204)
        self.assertEqual(self.lab.store.inbox_ids(), ["msg_lot_09"])
        self.assertEqual(self.lab.store.ledger_count(PARENT), 1)
        self.assertEqual(len(self.lab.store.executions), 1)
        received = self.lab.clock.now()
        self.assertEqual(
            self.lab.store.prune_inbox(received + SPEC_LADDER_LAST_SECONDS, INBOX_RETENTION_SECONDS),
            0,
        )
        self.assertEqual(self.lab.store.inbox_ids(), ["msg_lot_09"])
        self.assertEqual(
            self.lab.store.prune_inbox(received + INBOX_RETENTION_SECONDS, INBOX_RETENTION_SECONDS),
            1,
        )

    def test_whsec_round_trip_and_rotation(self) -> None:
        token = encode_whsec(LAB_WEBHOOK_SECRET)
        self.assertTrue(token.startswith("whsec_"))
        self.assertEqual(decode_whsec(token), LAB_WEBHOOK_SECRET)
        other = b"rotation-lab-hmac-key-0002-extra"
        self.lab.service.secrets = [LAB_WEBHOOK_SECRET, other]
        raw = canonical_bytes(_payload())
        timestamp = str(int(self.lab.clock.now()))
        signature = sign("msg_rot", timestamp, raw, other)
        response = _post(self.lab, raw, "msg_rot", timestamp, signature)
        self.assertEqual(response.status, 204)

        # Sender mid-rotation: an unknown old key, then a key the receiver holds.
        retired = b"retired-lab-hmac-key-0000-xxxxx"
        header = "v1a,ignored " + sign_rotation("msg_rot2", timestamp, raw, [retired, other])
        self.assertEqual(len(header.split(" ")), 3)
        rotated = _post(self.lab, raw, "msg_rot2", timestamp, header)
        self.assertEqual(rotated.status, 204)
        only_v1a = "v1a," + sign("msg_rot3", timestamp, raw, other).split(",", 1)[1]
        refused = _post(self.lab, raw, "msg_rot3", timestamp, only_v1a)
        self.assertEqual(refused.status, 401)
        self.assertEqual(self.lab.store.inbox_ids(), ["msg_rot", "msg_rot2"])

    def test_sender_retry_reaches_the_receiver_with_a_stable_id(self) -> None:
        attempts: list[dict] = []

        def transport(url: str, raw: bytes, headers: dict) -> Response:
            attempts.append(dict(headers))
            return self.lab.service.handle(
                Request(method="POST", path="/webhooks/events", headers=headers, body=raw)
            )

        self.lab.service.fail_webhook_commit = True
        sender = WebhookSender(
            endpoint=Endpoint("https://hooks.example/allotments"),
            clock=self.lab.clock,
            rng=self.lab.rng,
            journal=self.lab.journal,
            transport=transport,
            webhook_id="msg_lot_09",
        )
        delivery = sender.deliver(_payload())
        self.assertTrue(delivery.ok)
        self.assertEqual(delivery.attempts, 2)
        self.assertEqual(delivery.sleeps, [1.0])
        self.assertEqual({item["webhook-id"] for item in attempts}, {"msg_lot_09"})
        self.assertNotEqual(attempts[0]["webhook-timestamp"], attempts[1]["webhook-timestamp"])
        self.assertNotEqual(attempts[0]["webhook-signature"], attempts[1]["webhook-signature"])
        self.assertTrue(self.lab.journal.has("webhook_commit", "return_5xx_no_inbox"))
        self.assertTrue(self.lab.journal.has("webhook_status_500", "short_ladder"))
        self.assertEqual(self.lab.store.inbox_ids(), ["msg_lot_09"])
        self.assertEqual(len(self.lab.store.executions), 1)

        again = sender.deliver(_payload())
        self.assertTrue(again.ok)
        self.assertTrue(self.lab.journal.has("webhook_duplicate", "ack_without_write"))
        self.assertEqual(len(self.lab.store.executions), 1)

    def test_throttle_uses_full_jitter_and_the_budget_ends(self) -> None:
        statuses: list[int] = []

        def throttled(url: str, raw: bytes, headers: dict) -> Response:
            statuses.append(429)
            return Response(429, {}, b"")

        sender = WebhookSender(
            endpoint=Endpoint("https://hooks.example/allotments"),
            clock=self.lab.clock,
            rng=self.lab.rng,
            journal=self.lab.journal,
            transport=throttled,
            webhook_id="msg_throttle",
        )
        delivery = sender.deliver(_payload())
        self.assertFalse(delivery.ok)
        self.assertEqual(delivery.attempts, len(SHORT_LADDER_SECONDS))
        self.assertEqual(len(delivery.sleeps), len(SHORT_LADDER_SECONDS) - 1)
        for index, wait in enumerate(delivery.sleeps):
            self.assertGreaterEqual(wait, 0.0)
            self.assertLessEqual(wait, min(2.0, 0.05 * 2**index))
        self.assertTrue(self.lab.journal.has("webhook_status_429", "full_jitter_retry"))
        self.assertTrue(self.lab.journal.has("webhook_budget", "surface_last_error"))

    def test_spec_ladder_is_shorter_than_inbox_retention(self) -> None:
        self.assertEqual(sum(SPEC_LADDER_SECONDS), SPEC_LADDER_LAST_SECONDS)
        self.assertEqual(SPEC_LADDER_LAST_SECONDS, 75 * 3600 + 35 * 60 + 5)
        self.assertGreater(INBOX_RETENTION_SECONDS, SPEC_LADDER_LAST_SECONDS)
        self.assertLess(sum(SHORT_LADDER_SECONDS), SPEC_LADDER_LAST_SECONDS)

    def test_example_event_is_accepted_when_signed(self) -> None:
        raw = (ROOT / "examples" / "webhook_event.json").read_bytes()
        timestamp = str(int(self.lab.clock.now()))
        response = _post(self.lab, raw, "msg_example", timestamp, sign("msg_example", timestamp, raw, LAB_WEBHOOK_SECRET))
        self.assertEqual(response.status, 204)
        self.assertEqual(self.lab.store.ledger_get(PARENT, "lot-009")["note"], "revised")

    def test_sender_410_422_redirect_and_retry_after(self) -> None:
        endpoint = Endpoint("https://hooks.example/allotments")
        sender = WebhookSender(
            endpoint=endpoint,
            clock=self.lab.clock,
            rng=self.lab.rng,
            journal=self.lab.journal,
            transport=lambda url, raw, headers: Response(410, {}, b""),
            webhook_id="msg_stop",
        )
        stopped = sender.deliver(_payload())
        self.assertTrue(stopped.disabled)
        self.assertEqual(stopped.attempts, 1)
        self.assertTrue(endpoint.disabled)

        endpoint = Endpoint("https://hooks.example/allotments")
        sender = WebhookSender(
            endpoint=endpoint,
            clock=self.lab.clock,
            rng=self.lab.rng,
            journal=self.lab.journal,
            transport=lambda url, raw, headers: Response(422, {"content-type": "application/problem+json"}, b"{}"),
            webhook_id="msg_bad",
        )
        once = sender.deliver(_payload())
        self.assertEqual(once.attempts, 1)
        self.assertFalse(once.ok)

        urls: list[str] = []

        def redirect(url: str, raw: bytes, headers: dict) -> Response:
            urls.append(url)
            return Response(302, {"location": "https://elsewhere.example/hook"}, b"")

        endpoint = Endpoint("https://hooks.example/allotments")
        sender = WebhookSender(
            endpoint=endpoint,
            clock=self.lab.clock,
            rng=self.lab.rng,
            journal=self.lab.journal,
            transport=redirect,
            webhook_id="msg_redir",
        )
        redirected = sender.deliver(_payload())
        self.assertFalse(redirected.followed_redirect)
        self.assertTrue(urls)
        self.assertTrue(all(url == endpoint.url for url in urls))
        self.assertNotIn("https://elsewhere.example/hook", urls)

        calls = {"n": 0}

        def throttle(url: str, raw: bytes, headers: dict) -> Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return Response(503, {"Retry-After": "2"}, b"")
            return Response(204, {}, b"")

        endpoint = Endpoint("https://hooks.example/allotments")
        sender = WebhookSender(
            endpoint=endpoint,
            clock=self.lab.clock,
            rng=self.lab.rng,
            journal=self.lab.journal,
            transport=throttle,
            webhook_id="msg_wait",
        )
        waited = sender.deliver(_payload())
        self.assertTrue(waited.ok)
        self.assertEqual(waited.attempts, 2)
        self.assertEqual(waited.sleeps, [2.0])
        self.assertGreaterEqual(waited.sleeps[0], 2)
        self.assertLess(waited.sleeps[0], 4)


if __name__ == "__main__":
    unittest.main()
