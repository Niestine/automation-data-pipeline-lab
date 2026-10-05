"""Signature profiles: raw bytes, version allowlists, HMAC, and coverage."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from unittest import mock

import helpers
from quay_inbox.coverage import content_digest, sign_coverage, signature_base, verify_coverage
from quay_inbox.errors import AuthError
from quay_inbox.mac import (
    github_style_accepts,
    github_style_body_mac,
    prefix_sha256,
    sign_standard,
    sign_stripe,
    verify_standard,
    verify_stripe,
)
from quay_inbox.secrets import NEW_SECRET, OLD_SECRET, ROUTE_A, ROUTE_B, decode_whsec, encode_whsec


NOW = 1_700_000_100
BODY = helpers.standard_body()


class StandardMacTests(unittest.TestCase):
    def test_known_raw_body_verifies_and_reserializing_fails(self) -> None:
        signature = sign_standard(ROUTE_A, "msg_quay_0001", NOW, BODY)
        signed = f"msg_quay_0001.{NOW}.".encode("ascii") + BODY
        expected = hmac.new(ROUTE_A, signed, hashlib.sha256).digest()
        self.assertEqual(signature, "v1," + base64.b64encode(expected).decode("ascii"))
        headers = {
            "webhook-id": "msg_quay_0001",
            "webhook-timestamp": str(NOW),
            "webhook-signature": signature,
        }
        verified = verify_standard(BODY, headers, [ROUTE_A], now=NOW)
        self.assertEqual(verified.event_id, "msg_quay_0001")
        spaced = BODY.replace(b',"', b', "')
        reordered = json.dumps(json.loads(BODY), indent=2, sort_keys=False).encode("utf-8")
        self.assertNotEqual(reordered, BODY)
        with_newline = BODY + b"\n"
        for mutated in (spaced, reordered, with_newline):
            with self.assertRaises(AuthError):
                verify_standard(mutated, headers, [ROUTE_A], now=NOW)

    def test_dotted_id_or_timestamp_rejects_before_the_mac(self) -> None:
        headers = {
            "webhook-id": "msg.quay",
            "webhook-timestamp": str(NOW),
            "webhook-signature": "v1,AAAA",
        }
        with mock.patch("quay_inbox.mac.hmac.new", side_effect=AssertionError("mac")):
            with self.assertRaises(AuthError) as raised:
                verify_standard(BODY, headers, [ROUTE_A], now=NOW)
        self.assertEqual(raised.exception.code, "dotted")
        headers["webhook-id"] = "msg_quay_0001"
        headers["webhook-timestamp"] = f"{NOW}.0"
        with mock.patch("quay_inbox.mac.hmac.new", side_effect=AssertionError("mac")):
            with self.assertRaises(AuthError) as raised:
                verify_standard(BODY, headers, [ROUTE_A], now=NOW)
        self.assertEqual(raised.exception.code, "dotted")

    def test_version_allowlist_and_rotation(self) -> None:
        good = sign_standard(OLD_SECRET, "msg_rot_1", NOW, BODY)
        digest = good.split(",", 1)[1]
        only_v1a = {
            "webhook-id": "msg_rot_1",
            "webhook-timestamp": str(NOW),
            "webhook-signature": "v1a," + digest,
        }
        with self.assertRaises(AuthError):
            verify_standard(BODY, only_v1a, [OLD_SECRET, NEW_SECRET], now=NOW)
        only_old = dict(only_v1a)
        only_old["webhook-signature"] = sign_standard(OLD_SECRET, "msg_rot_1", NOW, BODY)
        only_new = dict(only_v1a)
        only_new["webhook-signature"] = sign_standard(NEW_SECRET, "msg_rot_1", NOW, BODY)
        self.assertEqual(
            verify_standard(BODY, only_old, [OLD_SECRET, NEW_SECRET], now=NOW).profile,
            "standard",
        )
        self.assertEqual(
            verify_standard(BODY, only_new, [NEW_SECRET, OLD_SECRET], now=NOW).event_id,
            "msg_rot_1",
        )
        with self.assertRaises(AuthError):
            verify_standard(BODY, only_old, [NEW_SECRET], now=NOW)
        self.assertEqual(
            verify_standard(BODY, only_new, [NEW_SECRET], now=NOW).event_id,
            "msg_rot_1",
        )

    def test_prefix_hash_and_wrong_route_fail(self) -> None:
        tag = base64.b64encode(prefix_sha256(ROUTE_A, BODY)).decode("ascii")
        headers = {
            "webhook-id": "msg_quay_0001",
            "webhook-timestamp": str(NOW),
            "webhook-signature": "v1," + tag,
        }
        with self.assertRaises(AuthError):
            verify_standard(BODY, headers, [ROUTE_A], now=NOW)
        foreign = {
            "webhook-id": "msg_quay_0001",
            "webhook-timestamp": str(NOW),
            "webhook-signature": sign_standard(ROUTE_B, "msg_quay_0001", NOW, BODY),
        }
        with self.assertRaises(AuthError):
            verify_standard(BODY, foreign, [ROUTE_A], now=NOW)
        garbage = dict(headers)
        garbage["webhook-signature"] = "v1,YQ v1,!!!!"
        with self.assertRaises(AuthError):
            verify_standard(BODY, garbage, [ROUTE_A], now=NOW)

    def test_tolerance_edges_and_zero_refused(self) -> None:
        def headers(timestamp: int) -> dict[str, str]:
            return {
                "webhook-id": "msg_quay_0001",
                "webhook-timestamp": str(timestamp),
                "webhook-signature": sign_standard(ROUTE_A, "msg_quay_0001", timestamp, BODY),
            }

        for delta in (299, -299, 300, -300):
            verified = verify_standard(BODY, headers(NOW + delta), [ROUTE_A], now=NOW)
            self.assertEqual(verified.timestamp, NOW + delta)
        for delta in (301, -301):
            with self.assertRaises(AuthError) as raised:
                verify_standard(BODY, headers(NOW + delta), [ROUTE_A], now=NOW)
            self.assertEqual(raised.exception.code, "stale")
        with self.assertRaises(ValueError):
            verify_standard(BODY, headers(NOW), [ROUTE_A], now=NOW, tolerance=0)

    def test_whsec_floor_is_32_bytes(self) -> None:
        short = base64.b64encode(b"0123456789abcdef01234567").decode("ascii")
        with self.assertRaises(ValueError):
            decode_whsec("whsec_" + short)
        self.assertEqual(decode_whsec(encode_whsec(ROUTE_A)), ROUTE_A)
        with self.assertRaises(ValueError):
            encode_whsec(b"x" * 65)


class StripeMacTests(unittest.TestCase):
    def test_v1_hex_ignores_v0_and_keeps_distinct_secrets(self) -> None:
        from quay_inbox.secrets import CLI_SECRET, DASHBOARD_SECRET

        body = helpers.stripe_body(
            "evt_1001",
            "release.granted",
            {"berth": "Q3", "id": "rel_1001", "object": "release", "status": "granted"},
            NOW,
        )
        header = sign_stripe(DASHBOARD_SECRET, NOW, body, include_v0=True)
        self.assertIn("v0=", header)
        verified = verify_stripe(body, header, [DASHBOARD_SECRET], now=NOW)
        self.assertEqual(verified.timestamp, NOW)
        v0_only = "t=%d,v0=%s" % (NOW, "ab" * 32)
        with self.assertRaises(AuthError):
            verify_stripe(body, v0_only, [DASHBOARD_SECRET], now=NOW)
        cli_header = sign_stripe(CLI_SECRET, NOW + 5, body)
        with self.assertRaises(AuthError):
            verify_stripe(body, cli_header, [DASHBOARD_SECRET], now=NOW + 5)
        self.assertEqual(
            verify_stripe(body, cli_header, [DASHBOARD_SECRET, CLI_SECRET], now=NOW + 5).profile,
            "stripe",
        )
        retry = sign_stripe(DASHBOARD_SECRET, NOW + 10, body)
        self.assertNotEqual(retry, header)
        self.assertIn(b'"id":"evt_1001"', body)

    def test_stripe_tolerance(self) -> None:
        from quay_inbox.secrets import DASHBOARD_SECRET

        body = b"{}"
        for delta in (299, -299, 300, -300):
            header = sign_stripe(DASHBOARD_SECRET, NOW + delta, body)
            verify_stripe(body, header, [DASHBOARD_SECRET], now=NOW)
        for delta in (301, -301):
            header = sign_stripe(DASHBOARD_SECRET, NOW + delta, body)
            with self.assertRaises(AuthError):
                verify_stripe(body, header, [DASHBOARD_SECRET], now=NOW)


class CoverageProfileTests(unittest.TestCase):
    def test_pinned_base_verifies_and_subset_or_digest_mismatch_fails(self) -> None:
        authority = "hooks.quay.example"
        path = "/hooks/coverage"
        headers = sign_coverage(
            ROUTE_A,
            method="POST",
            authority=authority,
            path=path,
            body=BODY,
            keyid="quay-coverage",
        )
        verify_coverage(
            method="POST",
            authority="Hooks.Quay.Example",
            path=path,
            body=BODY,
            headers=headers,
            secrets=[ROUTE_A],
            keyid="quay-coverage",
        )
        rest = headers["signature-input"].split("=", 1)[1]
        base = signature_base(
            method="POST",
            authority=authority,
            path=path,
            digest_header=headers["content-digest"],
            params_rest=rest,
        )
        expected = "\n".join(
            [
                '"@method": POST',
                '"@authority": hooks.quay.example',
                '"@path": /hooks/coverage',
                '"content-digest": ' + content_digest(BODY),
                '"@signature-params": ' + rest,
                "",
            ]
        ).encode("ascii")
        self.assertEqual(base, expected)
        flipped = bytearray(BODY)
        flipped[-1] ^= 0x01
        with self.assertRaises(AuthError):
            verify_coverage(
                method="POST",
                authority=authority,
                path=path,
                body=bytes(flipped),
                headers=headers,
                secrets=[ROUTE_A],
                keyid="quay-coverage",
            )
        hex_headers = dict(headers)
        hex_headers["content-digest"] = "sha-256=" + hashlib.sha256(BODY).hexdigest()
        with self.assertRaises(AuthError):
            verify_coverage(
                method="POST",
                authority=authority,
                path=path,
                body=BODY,
                headers=hex_headers,
                secrets=[ROUTE_A],
                keyid="quay-coverage",
            )
        mixed = dict(headers)
        mixed["content-digest"] = headers["content-digest"] + ", sha-512=:AAAA=:"
        with self.assertRaises(AuthError):
            verify_coverage(
                method="POST",
                authority=authority,
                path=path,
                body=BODY,
                headers=mixed,
                secrets=[ROUTE_A],
                keyid="quay-coverage",
            )
        subset_rest = '("@method" "@authority" "content-digest");alg="hmac-sha256";keyid="quay-coverage"'
        subset_base = "\n".join(
            [
                '"@method": POST',
                '"@authority": ' + authority,
                '"content-digest": ' + headers["content-digest"],
                '"@signature-params": ' + subset_rest,
                "",
            ]
        ).encode("ascii")
        subset_mac = base64.b64encode(hmac.new(ROUTE_A, subset_base, hashlib.sha256).digest()).decode("ascii")
        subset_headers = {
            "content-digest": headers["content-digest"],
            "signature-input": "quay=" + subset_rest,
            "signature": "quay=:" + subset_mac + ":",
        }
        with self.assertRaises(AuthError) as raised:
            verify_coverage(
                method="POST",
                authority=authority,
                path=path,
                body=BODY,
                headers=subset_headers,
                secrets=[ROUTE_A],
                keyid="quay-coverage",
            )
        self.assertEqual(raised.exception.code, "coverage")
        other_alg = dict(headers)
        other_alg["signature-input"] = headers["signature-input"].replace(
            'alg="hmac-sha256"', 'alg="hmac-sha512"'
        )
        with self.assertRaises(AuthError):
            verify_coverage(
                method="POST",
                authority=authority,
                path=path,
                body=BODY,
                headers=other_alg,
                secrets=[ROUTE_A],
                keyid="quay-coverage",
            )
        prefix = base64.b64encode(prefix_sha256(ROUTE_A, BODY)).decode("ascii")
        prefixed = dict(headers)
        prefixed["signature"] = "quay=:" + prefix + ":"
        with self.assertRaises(AuthError):
            verify_coverage(
                method="POST",
                authority=authority,
                path=path,
                body=BODY,
                headers=prefixed,
                secrets=[ROUTE_A],
                keyid="quay-coverage",
            )

    def test_repr_digest_and_obsolete_digest_do_not_satisfy(self) -> None:
        reserialized = json.dumps(json.loads(BODY)).encode("utf-8")
        digest = content_digest(reserialized)
        headers = {
            "repr-digest": digest,
            "signature-input": 'quay=("@method" "@authority" "@path" "content-digest");alg="hmac-sha256";keyid="quay-coverage"',
            "signature": "quay=:AAAA=:",
        }
        with self.assertRaises(AuthError):
            verify_coverage(
                method="POST",
                authority="hooks.quay.example",
                path="/hooks/coverage",
                body=BODY,
                headers=headers,
                secrets=[ROUTE_A],
                keyid="quay-coverage",
            )
        signed = sign_coverage(
            ROUTE_A,
            method="POST",
            authority="hooks.quay.example",
            path="/hooks/coverage",
            body=BODY,
        )
        signed["digest"] = "sha-256=" + hashlib.sha256(BODY).hexdigest()
        with self.assertRaises(AuthError):
            verify_coverage(
                method="POST",
                authority="hooks.quay.example",
                path="/hooks/coverage",
                body=BODY,
                headers=signed,
                secrets=[ROUTE_A],
                keyid="quay-coverage",
            )

    def test_github_style_body_mac_ignores_the_delivery_id(self) -> None:
        # A captured delivery replayed under a new unsigned delivery id: the
        # body MAC still verifies, so a dedupe map keyed on that header
        # treats it as a new event.
        mac = github_style_body_mac(ROUTE_A, BODY)
        seen_delivery_ids: set[str] = set()
        processed = 0
        for delivery_id in ("delivery-1", "delivery-2"):
            if github_style_accepts(ROUTE_A, BODY, mac) and delivery_id not in seen_delivery_ids:
                seen_delivery_ids.add(delivery_id)
                processed += 1
        self.assertEqual(processed, 2)
        self.assertFalse(github_style_accepts(ROUTE_A, BODY + b" ", mac))
        # Standard Webhooks puts the id inside the MAC, so the same swap fails.
        headers = {
            "webhook-id": "msg_quay_0001",
            "webhook-timestamp": str(NOW),
            "webhook-signature": sign_standard(ROUTE_A, "msg_quay_0001", NOW, BODY),
        }
        swapped = dict(headers)
        swapped["webhook-id"] = "msg_quay_9999"
        with self.assertRaises(AuthError):
            verify_standard(BODY, swapped, [ROUTE_A], now=NOW)


if __name__ == "__main__":
    unittest.main()
