import unittest

import helpers  # noqa: F401
from helpers import make_job, signed_deliveries

from api_reliability_lab.models import LAB_WEBHOOK_SECRET
from api_reliability_lab.seed import build_webhook_events
from api_reliability_lab.webhooks import build_delivery, sign_webhook


class WebhookTests(unittest.TestCase):
    def test_signed_snapshot_events_apply_dedup_and_stale_rules(self):
        job, _, _, logger, clock, _ = make_job()
        job.run(deliveries=[], dry_run=False)
        events = build_webhook_events()
        deliveries = signed_deliveries(events, clock)
        applied = []
        for headers, body in deliveries:
            result = job.receiver.handle(headers, body)
            applied.append(result.outcome)
        self.assertEqual(
            applied,
            ["applied", "duplicate", "duplicate", "stale", "applied", "applied"],
        )
        self.assertEqual(job.ledger.get("ORD-1001").status, "paid")
        self.assertEqual(job.ledger.get("ORD-1003").status, "cancelled")
        self.assertEqual(job.ledger.get("ORD-1025").status, "pending")
        self.assertGreater(job.ledger.get("ORD-1002").version, 1)
        self.assertTrue(any(item["event"] == "webhook_accepted" for item in logger.events))

    def test_bad_signature_is_rejected(self):
        job, _, _, _, clock, _ = make_job()
        event = build_webhook_events()[0]
        headers, body = build_delivery(event, secret="wrong-secret", timestamp=str(clock.now_s()))
        result = job.receiver.handle(headers, body)
        self.assertEqual(result.status, 401)
        self.assertEqual(result.outcome, "bad_signature")

    def test_missing_signature_is_rejected(self):
        job, _, _, _, clock, _ = make_job()
        event = build_webhook_events()[0]
        headers, body = build_delivery(
            event, secret=LAB_WEBHOOK_SECRET, timestamp=str(clock.now_s())
        )
        headers = dict(headers)
        del headers["x-webhook-signature"]
        result = job.receiver.handle(headers, body)
        self.assertEqual(result.status, 401)

    def test_non_ascii_signature_is_rejected_not_crashed(self):
        job, _, _, _, clock, _ = make_job()
        headers, body = build_delivery(
            build_webhook_events()[0], secret=LAB_WEBHOOK_SECRET, timestamp=str(clock.now_s())
        )
        headers["x-webhook-signature"] = "sha256=é"
        result = job.receiver.handle(headers, body)
        self.assertEqual(result.status, 401)
        self.assertEqual(result.outcome, "bad_signature")

    def test_non_ascii_digit_timestamp_is_rejected(self):
        job, *_ = make_job()
        headers, body = build_delivery(
            build_webhook_events()[0], secret=LAB_WEBHOOK_SECRET, timestamp="²"
        )
        result = job.receiver.handle(headers, body)
        self.assertEqual(result.status, 401)
        self.assertEqual(result.outcome, "replay")

    def test_signed_non_utf8_body_is_a_400(self):
        job, _, _, _, clock, _ = make_job()
        headers, body = build_delivery(
            {}, secret=LAB_WEBHOOK_SECRET, timestamp=str(clock.now_s()), body=b"\xff\xfe"
        )
        result = job.receiver.handle(headers, body)
        self.assertEqual(result.status, 400)
        self.assertEqual(result.outcome, "invalid_schema")

    def test_replay_window_rejects_old_and_future_timestamps(self):
        job, _, _, _, clock, _ = make_job()
        event = build_webhook_events()[0]
        old_ts = str(clock.now_s() - 301)
        headers, body = build_delivery(event, secret=LAB_WEBHOOK_SECRET, timestamp=old_ts)
        result = job.receiver.handle(headers, body)
        self.assertEqual(result.outcome, "replay")
        future_ts = str(clock.now_s() + 301)
        headers, body = build_delivery(event, secret=LAB_WEBHOOK_SECRET, timestamp=future_ts)
        result = job.receiver.handle(headers, body)
        self.assertEqual(result.outcome, "replay")

    def test_receiver_uses_raw_body_bytes_for_hmac(self):
        job, _, _, _, clock, _ = make_job()
        event = build_webhook_events()[0]
        ts = str(clock.now_s())
        headers, body = build_delivery(event, secret=LAB_WEBHOOK_SECRET, timestamp=ts)
        mutated = body + b" "
        headers["x-webhook-signature"] = sign_webhook(LAB_WEBHOOK_SECRET, ts, body)
        result = job.receiver.handle(headers, mutated)
        self.assertEqual(result.outcome, "bad_signature")

    def test_invalid_schema_returns_400(self):
        job, _, _, _, clock, _ = make_job()
        ts = str(clock.now_s())
        body = b'{"id":"evt_0099","type":"order.updated","created_at":"2026-01-02T12:00:00Z","data":{}}'
        headers = {
            "x-webhook-timestamp": ts,
            "x-webhook-signature": sign_webhook(LAB_WEBHOOK_SECRET, ts, body),
        }
        result = job.receiver.handle(headers, body)
        self.assertEqual(result.status, 400)
        self.assertEqual(result.outcome, "invalid_schema")

    def test_duplicate_still_returns_200_so_the_sender_stops_retrying(self):
        job, _, _, _, clock, _ = make_job()
        job.run(deliveries=[])
        event = build_webhook_events()[0]
        headers, body = build_delivery(event, secret=LAB_WEBHOOK_SECRET, timestamp=str(clock.now_s()))
        first = job.receiver.handle(headers, body)
        second = job.receiver.handle(headers, body)
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(second.outcome, "duplicate")

    def test_dry_run_does_not_record_event_ids(self):
        job, _, _, _, clock, _ = make_job()
        event = build_webhook_events()[0]
        headers, body = build_delivery(event, secret=LAB_WEBHOOK_SECRET, timestamp=str(clock.now_s()))
        result = job.receiver.handle(headers, body, dry_run=True)
        self.assertEqual(result.outcome, "applied")
        self.assertFalse(job.ledger.seen_event("evt_0001"))
        self.assertIsNone(job.ledger.get("ORD-1001"))


if __name__ == "__main__":
    unittest.main()
