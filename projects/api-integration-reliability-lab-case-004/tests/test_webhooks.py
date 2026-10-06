"""Raw-body webhook signatures and the compressed delivery schedule."""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path

from helpers import ROOT, LabCase
from lotcycle.clock import VirtualClock
from lotcycle.httputil import Request
from lotcycle.params import RECEIVER_TIMEOUT, WEBHOOK_RETENTION, WEBHOOK_TOLERANCE
from lotcycle.retry import full_jitter
from lotcycle.schema import canonical_json
from lotcycle.webhooks import (
    WebhookProducer,
    decode_whsec,
    encode_whsec,
    endpoint_secret,
    mac_bytes,
    sign,
    signature_header,
)
import random


class _Delivery:
    def __init__(self, status: int, headers: dict | None = None, timed_out: bool = False) -> None:
        self.status = status
        self.headers = headers or {}
        self.timed_out = timed_out


class WebhookSignatureTest(LabCase):
    def _post(self, body: bytes, webhook_id: str, timestamp: str, signature: str, endpoint: str = "desk-north"):
        return self.lab.hooks.handle(
            Request(
                "POST",
                "/hooks/" + endpoint,
                {
                    "content-type": "application/json",
                    "webhook-id": webhook_id,
                    "webhook-signature": signature,
                    "webhook-timestamp": timestamp,
                },
                body,
            )
        )

    def test_mac_covers_the_raw_body(self) -> None:
        secret = endpoint_secret("desk-north")
        self.lab.hooks.trust("desk-north", [secret])
        body = canonical_json(
            {"occurred_at": 120, "resource_id": "ex-120", "type": "excursion.opened"}
        )
        timestamp = str(int(self.lab.clock.now()))
        signature = sign(secret, "evt-1", timestamp, body)
        encoded = signature.split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), mac_bytes(secret, "evt-1", timestamp, body))
        accepted = self._post(body, "evt-1", timestamp, signature)
        self.assertEqual(accepted.status, 204)
        self.assertEqual(self.lab.hooks.handler_runs, 1)

        changed = bytearray(body)
        changed[0] ^= 0x01
        self.assertEqual(
            self._post(bytes(changed), "evt-2", timestamp, sign(secret, "evt-2", timestamp, body)).status,
            401,
        )
        reserialized = json.dumps(json.loads(body)).encode("utf-8")
        self.assertNotEqual(reserialized, body)
        self.assertEqual(
            self._post(
                reserialized,
                "evt-3",
                timestamp,
                sign(secret, "evt-3", timestamp, body),
            ).status,
            401,
        )
        late = str(int(self.lab.clock.now()) + WEBHOOK_TOLERANCE + 1)
        early = str(int(self.lab.clock.now()) - WEBHOOK_TOLERANCE - 1)
        self.assertEqual(self._post(body, "evt-late", late, sign(secret, "evt-late", late, body)).status, 401)
        self.assertEqual(
            self._post(body, "evt-early", early, sign(secret, "evt-early", early, body)).status,
            401,
        )
        edge = str(int(self.lab.clock.now()) + WEBHOOK_TOLERANCE)
        self.assertEqual(self._post(body, "evt-edge", edge, sign(secret, "evt-edge", edge, body)).status, 204)
        self.assertEqual(self.lab.store.webhook_count("desk-north"), 2)

    def test_replay_keeps_one_handler_run_until_retention(self) -> None:
        secret = endpoint_secret("desk-north")
        self.lab.hooks.trust("desk-north", [secret])
        body = canonical_json({"occurred_at": 1, "resource_id": "ex-001", "type": "excursion.opened"})

        def deliver() -> int:
            timestamp = str(int(self.lab.clock.now()))
            return self._post(body, "evt-same", timestamp, sign(secret, "evt-same", timestamp, body)).status

        self.assertEqual(deliver(), 204)
        self.lab.clock.advance(WEBHOOK_RETENTION)
        self.assertEqual(deliver(), 204)
        self.assertEqual(self.lab.hooks.handler_runs, 1)
        self.lab.clock.advance(1)
        self.assertEqual(deliver(), 204)
        self.assertEqual(self.lab.hooks.handler_runs, 2)

    def test_rotation_accepts_either_current_secret(self) -> None:
        old = endpoint_secret("desk-north")
        new = endpoint_secret("desk-rotated")
        other = endpoint_secret("desk-south")
        body = canonical_json({"occurred_at": 1, "resource_id": "ex-001", "type": "excursion.opened"})
        timestamp = str(int(self.lab.clock.now()))
        self.lab.hooks.trust("desk-north", [old, new])
        both = signature_header([old, new], "evt-rot", timestamp, body)
        self.assertEqual(self._post(body, "evt-rot", timestamp, both).status, 204)

        self.lab.hooks.trust("desk-north", [old])
        self.assertEqual(
            self._post(body, "evt-old", timestamp, sign(old, "evt-old", timestamp, body)).status,
            204,
        )
        self.lab.hooks.trust("desk-north", [new])
        self.assertEqual(
            self._post(body, "evt-new", timestamp, sign(new, "evt-new", timestamp, body)).status,
            204,
        )
        self.assertEqual(
            self._post(body, "evt-miss", timestamp, sign(old, "evt-miss", timestamp, body)).status,
            401,
        )
        self.lab.hooks.trust("desk-north", [other])
        self.assertEqual(
            self._post(
                body,
                "evt-unknown",
                timestamp,
                signature_header([old, new], "evt-unknown", timestamp, body),
            ).status,
            401,
        )
        self.lab.hooks.trust("desk-north", [old])
        mac = mac_bytes(old, "evt-version", timestamp, body)
        only_v1a = "v1a," + base64.b64encode(mac).decode("ascii")
        self.assertEqual(self._post(body, "evt-version", timestamp, only_v1a).status, 401)
        mixed = only_v1a + " " + sign(old, "evt-version", timestamp, body)
        self.assertEqual(self._post(body, "evt-version", timestamp, mixed).status, 204)
        self.assertEqual(self.lab.store.webhook_count("desk-north"), 4)

    def test_malformed_id_and_oversize_body_are_rejected_before_storage(self) -> None:
        secret = endpoint_secret("desk-north")
        self.lab.hooks.trust("desk-north", [secret])
        body = canonical_json({"occurred_at": 1, "resource_id": "ex-001", "type": "excursion.opened"})
        timestamp = str(int(self.lab.clock.now()))
        dotted = self._post(body, "evt.bad", timestamp, sign(secret, "evt.bad", timestamp, body))
        self.assertEqual(dotted.status, 400)
        dotted_time = self._post(body, "evt-time", "12.0", sign(secret, "evt-time", "12.0", body))
        self.assertEqual(dotted_time.status, 400)
        letters = self._post(body, "evt-letters", "12ab", sign(secret, "evt-letters", "12ab", body))
        self.assertEqual(letters.status, 400)
        huge = b"{" + (b"a" * 20480)
        self.assertGreater(len(huge), 20480)
        oversize = self._post(huge, "evt-huge", timestamp, "v1,aaaa")
        self.assertEqual(oversize.status, 413)
        invalid = canonical_json({"occurred_at": 1, "resource_id": "ex-001", "type": "nope"})
        # type must contain a dot; the signature is valid and the id is not stored.
        signed = sign(secret, "evt-schema", timestamp, invalid)
        self.assertEqual(self._post(invalid, "evt-schema", timestamp, signed).status, 400)
        self.assertEqual(self.lab.store.webhook_count("desk-north"), 0)

    def test_reader_failure_rolls_back_the_seen_id(self) -> None:
        secret = endpoint_secret("desk-north")
        self.lab.hooks.trust("desk-north", [secret])
        body = canonical_json({"occurred_at": 1, "resource_id": "ex-001", "type": "excursion.opened"})
        timestamp = str(int(self.lab.clock.now()))

        def reader(resource_id: str) -> bool:
            self.lab.store.con.execute(
                """
                INSERT INTO webhook_seen (endpoint_id, webhook_id, received_at, resource_id)
                VALUES (?, ?, ?, ?)
                """,
                ("desk-north", "evt-inner", 1, resource_id),
            )
            raise RuntimeError("reader failed")

        self.lab.hooks.reader = reader
        with self.assertRaises(RuntimeError):
            self._post(body, "evt-outer", timestamp, sign(secret, "evt-outer", timestamp, body))
        self.assertEqual(self.lab.store.webhook_count("desk-north"), 0)

    def test_examples_do_not_embed_webhook_secrets(self) -> None:
        for path in (ROOT / "examples").glob("*.json"):
            self.assertNotIn("whsec_", path.read_text(encoding="utf-8"))
        secret = endpoint_secret("desk-north")
        encoded = encode_whsec(secret)
        self.assertTrue(encoded.startswith("whsec_"))
        self.assertEqual(decode_whsec(encoded), secret)
        with self.assertRaises(ValueError):
            encode_whsec(b"x" * 16)


class DeliveryTest(LabCase):
    def test_success_redirect_retirement_and_retry_after(self) -> None:
        clock = VirtualClock(1_000)
        secret = endpoint_secret("desk-north")
        producer = WebhookProducer(
            clock,
            random.Random(1),
            "desk-north",
            secret,
            "https://handheld.coldlot.example/hooks/desk-north",
        )
        seen: list[dict] = []

        def ok(attempt: dict) -> _Delivery:
            seen.append(attempt)
            return _Delivery(200)

        done = producer.deliver("evt-ok", b"{}", ok)
        self.assertEqual(done["attempts"], 1)
        self.assertEqual(done["status"], 200)
        self.assertEqual(seen[0]["timeout"], RECEIVER_TIMEOUT)

        def redirect(attempt: dict) -> _Delivery:
            seen.append(attempt)
            if len(seen) == 1:
                return _Delivery(302, {"Location": "https://elsewhere.example/landed"})
            return _Delivery(200)

        seen.clear()
        followed = producer.deliver("evt-redirect", b"{}", redirect)
        self.assertEqual(followed["attempts"], 2)
        self.assertEqual(followed["urls"], [producer.url, producer.url])
        self.assertEqual(producer.redirects, ["https://elsewhere.example/landed"])
        self.assertTrue(all(item["url"] == producer.url for item in seen))
        self.assertTrue(all(item["timeout"] == 15 for item in seen))

        calls: list[int] = []

        def gone(attempt: dict) -> _Delivery:
            calls.append(1)
            return _Delivery(410)

        retired = producer.deliver("evt-gone", b"{}", gone)
        self.assertEqual(retired["status"], 410)
        self.assertTrue(producer.disabled)
        quiet = producer.deliver("evt-later", b"{}", gone)
        self.assertEqual(quiet["status"], "disabled")
        self.assertEqual(calls, [1])

    def test_retry_after_overrides_jitter(self) -> None:
        clock = VirtualClock(2_000)
        producer = WebhookProducer(
            clock,
            random.Random(1),
            "desk-north",
            endpoint_secret("desk-north"),
            "https://handheld.coldlot.example/hooks/desk-north",
        )
        predicted = full_jitter(random.Random(1), 0)
        self.assertNotEqual(predicted, 7)
        calls = {"n": 0}

        def receiver(attempt: dict) -> _Delivery:
            calls["n"] += 1
            if calls["n"] == 1:
                return _Delivery(429, {"Retry-After": "7"})
            return _Delivery(200)

        start = clock.now()
        result = producer.deliver("evt-slow", b"{}", receiver)
        self.assertEqual(result["status"], 200)
        self.assertEqual(clock.now() - start, 7)

        fresh = VirtualClock(0)
        negative = WebhookProducer(
            fresh,
            random.Random(1),
            "desk-north",
            endpoint_secret("desk-north"),
            "https://handheld.coldlot.example/hooks/desk-north",
        )
        predicted_negative = full_jitter(random.Random(1), 0)
        state = {"n": 0}

        def bad_after(attempt: dict) -> _Delivery:
            state["n"] += 1
            if state["n"] == 1:
                return _Delivery(429, {"Retry-After": "-1"})
            return _Delivery(200)

        negative.deliver("evt-neg", b"{}", bad_after)
        self.assertEqual(fresh.now(), predicted_negative)
        self.assertGreater(predicted_negative, 0)

        for value in ("inf", "nan", "Wed, 21 Oct 2026 07:28:00 GMT"):
            with self.subTest(retry_after=value):
                clock_value = VirtualClock(0)
                odd = WebhookProducer(
                    clock_value,
                    random.Random(1),
                    "desk-north",
                    endpoint_secret("desk-north"),
                    "https://handheld.coldlot.example/hooks/desk-north",
                )
                replies = iter([_Delivery(429, {"Retry-After": value}), _Delivery(200)])
                odd.deliver("evt-odd", b"{}", lambda attempt: next(replies))
                self.assertEqual(clock_value.now(), full_jitter(random.Random(1), 0))

    def test_five_failures_stop_and_timeout_counts(self) -> None:
        clock = VirtualClock(0)
        producer = WebhookProducer(
            clock,
            random.Random(2),
            "desk-north",
            endpoint_secret("desk-north"),
            "https://handheld.coldlot.example/hooks/desk-north",
        )
        exhausted = producer.deliver("evt-500", b"{}", lambda attempt: _Delivery(500))
        self.assertEqual(exhausted["attempts"], 5)
        self.assertEqual(exhausted["status"], 500)
        self.assertFalse(producer.disabled)

        producer.disabled = False
        steps = {"n": 0}

        def timeout(attempt: dict) -> _Delivery:
            steps["n"] += 1
            if steps["n"] == 1:
                return _Delivery(200, timed_out=True)
            return _Delivery(200)

        recovered = producer.deliver("evt-timeout", b"{}", timeout)
        self.assertEqual(recovered["attempts"], 2)
        self.assertEqual(recovered["status"], 200)

    def test_one_in_flight_delivery_caps_the_second(self) -> None:
        clock = VirtualClock(0)
        producer = WebhookProducer(
            clock,
            random.Random(4),
            "desk-north",
            endpoint_secret("desk-north"),
            "https://handheld.coldlot.example/hooks/desk-north",
            max_in_flight=1,
        )
        started = threading.Event()
        release = threading.Event()

        def stalled(attempt: dict) -> _Delivery:
            started.set()
            release.wait(2)
            return _Delivery(200)

        holder: list[dict] = []

        def run() -> None:
            holder.append(producer.deliver("evt-hold", b"{}", stalled))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            capped = producer.deliver("evt-cap", b"{}", lambda attempt: _Delivery(200))
            self.assertEqual(capped["status"], "capped")
            self.assertEqual(producer.max_seen, 1)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder[0]["status"], 200)

    def test_retry_keeps_the_webhook_id(self) -> None:
        clock = VirtualClock(50)
        producer = WebhookProducer(
            clock,
            random.Random(6),
            "desk-north",
            endpoint_secret("desk-north"),
            "https://handheld.coldlot.example/hooks/desk-north",
        )
        headers: list[dict] = []

        def receiver(attempt: dict) -> _Delivery:
            headers.append(attempt["headers"])
            if len(headers) == 1:
                return _Delivery(500, {"Retry-After": "2"})
            return _Delivery(200)

        producer.deliver("evt-stable", b'{"ok":true}', receiver)
        self.assertEqual(headers[0]["webhook-id"], "evt-stable")
        self.assertEqual(headers[1]["webhook-id"], "evt-stable")
        self.assertNotEqual(headers[0]["webhook-timestamp"], headers[1]["webhook-timestamp"])
        self.assertNotEqual(headers[0]["webhook-signature"], headers[1]["webhook-signature"])


class ExamplePathTest(LabCase):
    def test_example_directory_is_inside_the_project(self) -> None:
        self.assertTrue((Path(ROOT) / "examples" / "webhook_event.json").is_file())
