"""Issuer, audience, and client binding on access tokens and refresh tokens.

Fett, Küsters, and Schmitz (CCS 2016) give authorization and
session-integrity goals for several grants at once. Their 2016 proof does
not expire access tokens, does not model revocation, and does not prove
refresh-family replay. These checks are the fail-closed shape only.
"""

from __future__ import annotations

import json

from helpers import LabCase, refresh_request
from lotcycle.httputil import Request
from lotcycle.tokens import b64url, b64url_decode, issue_access, unverified_payload


class AudienceTest(LabCase):
    def test_access_token_is_bound_to_issuer_audience_and_expiry(self) -> None:
        north = self.north()
        token = north.access_token
        ledger = self.lab.resources["lot-ledger"]
        board = self.lab.resources["alarm-board"]
        accepted = ledger.handle(
            Request("GET", "/lot-ledger/entries", {"authorization": "Bearer " + token})
        )
        self.assertEqual(accepted.status, 200)
        foreign = board.handle(
            Request("GET", "/alarm-board/entries", {"authorization": "Bearer " + token})
        )
        self.assertEqual(foreign.status, 401)

        raw_part, mac_part = token.split(".", 1)
        payload = unverified_payload(token)
        payload["aud"] = "alarm-board"
        mutated = b64url(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        forged_aud = mutated + "." + mac_part
        self.assertEqual(
            board.handle(
                Request("GET", "/alarm-board/entries", {"authorization": "Bearer " + forged_aud})
            ).status,
            401,
        )

        payload = unverified_payload(token)
        payload["iss"] = "https://other.example"
        wrong_iss = issue_access(payload)
        self.assertEqual(
            ledger.handle(
                Request("GET", "/lot-ledger/entries", {"authorization": "Bearer " + wrong_iss})
            ).status,
            401,
        )

        payload = unverified_payload(token)
        payload["exp"] = self.lab.clock.now()
        expired = issue_access(payload)
        self.assertEqual(
            ledger.handle(
                Request("GET", "/lot-ledger/entries", {"authorization": "Bearer " + expired})
            ).status,
            401,
        )

        mac = bytearray(b64url_decode(mac_part))
        mac[0] ^= 0x01
        flipped = raw_part + "." + b64url(bytes(mac))
        before = self.lab.store.idempotency_count()
        denied = ledger.handle(
            Request(
                "POST",
                "/lot-ledger/orders",
                {
                    "authorization": "Bearer " + flipped,
                    "content-type": "application/json",
                    "idempotency-key": '"aud-key"',
                },
                b'{"qty":2,"sku":"crate-ice"}',
            )
        )
        self.assertEqual(denied.status, 401)
        self.assertEqual(self.lab.store.idempotency_count(), before)

    def test_two_grants_do_not_share_entry_ids(self) -> None:
        north_ids = self.north().sync()
        south_ids = self.lab.clients["handheld-south"].sync()
        self.assertTrue(all(item.startswith("ex-") for item in north_ids))
        self.assertEqual(south_ids, {"al-001", "al-002", "al-003"})
        self.assertTrue(north_ids.isdisjoint(south_ids))
        self.assertEqual(self.lab.store.checkpoint_count("handheld-south", "alarm-board"), 0)
        self.assertTrue(self.lab.clients["handheld-south"].sync_complete)

    def test_other_confidential_client_cannot_refresh_the_grant(self) -> None:
        """C2's basic credentials presenting C1's refresh token fail closed.

        The family stays active. See the module docstring for the limits of
        the 2016 proof this check does not extend.
        """
        north = self.north()
        south = self.lab.clients["handheld-south"]
        denied = self.lab.auth.handle(refresh_request(south, refresh=north.refresh_token))
        self.assertEqual(denied.status, 400)
        self.assertEqual(denied.json()["error"], "invalid_grant")
        family = self.family()
        self.assertEqual(family["status"], "active")
        self.assertEqual(family["active_generation"], 1)
        self.assertEqual(self.lab.store.reuse_count(north.family_id), 0)
        self.assertEqual(north.status, "active")
        self.assertEqual(south.status, "active")
