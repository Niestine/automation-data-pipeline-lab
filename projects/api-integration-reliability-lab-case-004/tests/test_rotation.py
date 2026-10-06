"""Refresh-token families, the closed token-endpoint error set, and scope."""

from __future__ import annotations

from helpers import (
    LabCase,
    logs_text,
    refresh_request,
    refresh_row_count,
    order_request,
)
from lotcycle.auth_server import RegistrationError
from lotcycle.errors import TokenEndpointError
from lotcycle.httputil import Request
from lotcycle.params import ACCESS_LIFETIME, INACTIVITY_WINDOW, TERMINAL_ERRORS
from lotcycle.tokens import b64url_decode, new_refresh_token, read_access, token_hash


class RotationTest(LabCase):
    def test_replay_revokes_the_family_whichever_party_goes_first(self) -> None:
        # The public kiosk has no secret, so a thief needs only the stolen
        # refresh token and the public client_id. The server cannot tell the
        # parties apart; whichever presents the ancestor second ends the family.
        client_first = self._replay_terminal(thief_first=False)
        thief_first = self._replay_terminal(thief_first=True)
        self.assertEqual(client_first, thief_first)
        self.assertEqual(
            client_first,
            {
                "active": 0,
                "client_status": "reauth_required",
                "generation": None,
                "reuse": 1,
                "status": "reauth_required",
                "thief_denied": True,
            },
        )

    def _replay_terminal(self, thief_first: bool) -> dict:
        from helpers import build

        lab = build()
        try:
            client = lab.clients["kiosk-public"]
            stolen = client.refresh_token
            if thief_first:
                rotated = lab.auth.handle(refresh_request(client, refresh=stolen))
                self.assertEqual(rotated.status, 200)
                self.assert_nostore(rotated)
                thief_holds = rotated.json()["refresh_token"]
                self.assertNotEqual(thief_holds, stolen)
                # The legitimate client still holds generation 1 and presents it.
                with self.assertRaises(TokenEndpointError) as caught:
                    client.refresh()
                self.assertEqual(caught.exception.error, "invalid_grant")
            else:
                client.refresh()
                self.assertNotEqual(client.refresh_token, stolen)
                replay = lab.auth.handle(refresh_request(client, refresh=stolen))
                self.assertEqual(replay.status, 400)
                self.assertEqual(replay.json()["error"], "invalid_grant")
                self.assert_nostore(replay)
                thief_holds = stolen
                # The legitimate client's descendant was revoked by the replay.
                with self.assertRaises(TokenEndpointError):
                    client.refresh()
            family = lab.store.get_family(client.family_id)
            reuse_logs = [row for row in lab.store.logs if row["event"] == "refresh_reuse_detected"]
            self.assertEqual(len(reuse_logs), 1)
            self.assertEqual(reuse_logs[0]["presenter"], client.client_id)
            self.assertEqual(reuse_logs[0]["generation"], 1)
            denied = lab.auth.handle(refresh_request(client, refresh=thief_holds))
            return {
                "active": lab.store.active_refresh_count(client.family_id),
                "client_status": client.status,
                "generation": family["active_generation"],
                "reuse": lab.store.reuse_count(client.family_id),
                "status": family["status"],
                "thief_denied": denied.json().get("error") == "invalid_grant",
            }
        finally:
            lab.close()

    def test_concurrent_exchange_of_one_generation_is_a_replay(self) -> None:
        """Two holders race the same generation past the read-side checks.

        The second exchange is forced to start after the first passed its
        checks and before it committed. The compare-and-rotate update fails
        for whichever commits second, and that presentation is the replay.
        """
        client = self.north()
        held = client.refresh_token
        auth = self.lab.auth
        original = auth._access_token
        inner: list = []
        raced: list[bool] = []

        def racing_access_token(*args, **kwargs):
            if not raced:
                raced.append(True)
                inner.append(auth.handle(refresh_request(client, refresh=held)))
            return original(*args, **kwargs)

        auth._access_token = racing_access_token
        try:
            outer = auth.handle(refresh_request(client, refresh=held))
        finally:
            auth._access_token = original
        self.assertEqual(inner[0].status, 200)
        self.assertEqual(outer.status, 400)
        self.assertEqual(outer.json()["error"], "invalid_grant")
        self.assert_nostore(outer)
        family = self.family()
        self.assertEqual(family["status"], "reauth_required")
        self.assertIsNone(family["active_generation"])
        self.assertEqual(self.lab.store.reuse_count(client.family_id), 1)
        self.assertEqual(self.lab.store.active_refresh_count(client.family_id), 0)

    def test_other_client_cannot_revoke_by_presenting_the_token(self) -> None:
        """Binding is checked before replay, including a consumed generation.

        Fett, Küsters, and Schmitz (CCS 2016) state authorization and
        session-integrity goals for simultaneous grants. Their 2016 proof
        does not expire access tokens, does not model revocation, and does
        not prove refresh-family replay. This test only checks the
        fail-closed shape: another confidential client presenting the
        victim's refresh token gets invalid_grant and the family stays active.
        """
        north = self.north()
        south = self.lab.clients["handheld-south"]
        stolen = north.refresh_token
        denied = self.lab.auth.handle(refresh_request(south, refresh=stolen))
        self.assertEqual(denied.json()["error"], "invalid_grant")
        self.assertEqual(self.family()["status"], "active")
        self.assertEqual(self.family()["active_generation"], 1)
        self.assertEqual(self.lab.store.reuse_count(north.family_id), 0)
        self.assertEqual(north.status, "active")

        rotated = self.lab.auth.handle(refresh_request(north, refresh=stolen))
        self.assertEqual(rotated.status, 200)
        descendant = rotated.json()["refresh_token"]
        denied_new = self.lab.auth.handle(refresh_request(south, refresh=descendant))
        self.assertEqual(denied_new.json()["error"], "invalid_grant")
        self.assertEqual(self.family()["active_generation"], 2)
        self.assertEqual(self.family()["status"], "active")
        self.assertEqual(self.lab.store.reuse_count(north.family_id), 0)

        denied_old = self.lab.auth.handle(refresh_request(south, refresh=stolen))
        self.assertEqual(denied_old.json()["error"], "invalid_grant")
        self.assertEqual(self.family()["status"], "active")
        self.assertEqual(self.family()["active_generation"], 2)
        self.assertEqual(self.lab.store.active_refresh_count(north.family_id), 1)
        self.assertEqual(self.lab.store.reuse_count(north.family_id), 0)

    def test_section_52_codes_are_emitted_and_do_not_mint(self) -> None:
        client = self.north()
        before = refresh_row_count(self.lab)
        missing_grant = self.lab.auth.handle(
            Request("POST", "/token", {}, b"refresh_token=abc")
        )
        self.assertEqual(missing_grant.json()["error"], "invalid_request")
        missing_token = self.lab.auth.handle(
            refresh_request(client, include_refresh=False)
        )
        self.assertEqual(missing_token.json()["error"], "invalid_request")
        repeated = self.lab.auth.handle(refresh_request(client, repeated=True))
        self.assertEqual(repeated.json()["error"], "invalid_request")
        self.assertEqual(self.family()["active_generation"], 1)

        bad_secret = self.lab.auth.handle(refresh_request(client, secret="not-the-secret"))
        self.assertEqual(bad_secret.status, 401)
        self.assertEqual(bad_secret.json()["error"], "invalid_client")
        self.assertEqual(bad_secret.headers.get("www-authenticate"), 'Basic realm="token"')
        self.assert_nostore(bad_secret)
        no_basic = self.lab.auth.handle(refresh_request(client, basic_auth=False))
        self.assertEqual(no_basic.status, 401)
        self.assertEqual(no_basic.json()["error"], "invalid_client")

        self.lab.store.con.execute(
            "UPDATE clients SET refresh_allowed = 0 WHERE client_id = ?",
            (client.client_id,),
        )
        blocked = self.lab.auth.handle(refresh_request(client))
        self.assertEqual(blocked.json()["error"], "unauthorized_client")
        self.assertIsNone(self.lab.store.find_refresh(token_hash(client.refresh_token))["consumed_at"])
        self.lab.store.con.execute(
            "UPDATE clients SET refresh_allowed = 1 WHERE client_id = ?",
            (client.client_id,),
        )

        unknown = self.lab.auth.handle(refresh_request(client, refresh="unknown-token-value"))
        self.assertEqual(unknown.json()["error"], "invalid_grant")
        self.assertEqual(self.family()["active_generation"], 1)
        self.assertEqual(self.lab.store.reuse_count(client.family_id), 0)

        for grant in ("password", "implicit", "client_credentials"):
            refused = self.lab.auth.handle(refresh_request(client, grant=grant, include_refresh=False))
            self.assertEqual(refused.json()["error"], "unsupported_grant_type")
            self.assert_nostore(refused)
        self.assertEqual(refresh_row_count(self.lab), before)

        extra = self.lab.auth.handle(
            refresh_request(client, scope="excursions.read orders.write extra")
        )
        self.assertEqual(extra.status, 400)
        self.assertEqual(extra.json()["error"], "invalid_scope")
        self.assert_nostore(extra)
        self.assertEqual(self.family()["active_generation"], 1)
        saved = client.access_token
        posted = self.lab.resources["lot-ledger"].handle(
            order_request(saved, {"qty": 2, "sku": "crate-ice"}, "scope-still-good")
        )
        self.assertEqual(posted.status, 201)
        payload = read_access(saved)
        self.assertEqual(payload["scope"], "excursions.read orders.write")

        self.lab.auth.fail_before_commit = 1
        rolled = self.lab.auth.handle(refresh_request(client))
        self.assertEqual(rolled.status, 500)
        self.assertEqual(rolled.json()["error"], "server_error")
        self.assert_nostore(rolled)
        self.assertEqual(self.family()["active_generation"], 1)

    def test_client_stops_after_one_terminal_response(self) -> None:
        cases = (
            ("invalid_request", lambda lab, client: setattr(client, "refresh_token", "")),
            ("invalid_client", lambda lab, client: setattr(client, "secret", "wrong-secret")),
            ("invalid_grant", lambda lab, client: setattr(client, "refresh_token", "unknown-token-value")),
            ("unauthorized_client", _disallow_refresh),
            ("unsupported_grant_type", _force_grant("password")),
            ("unsupported_grant_type", _force_grant("implicit")),
            ("unsupported_grant_type", _force_grant("client_credentials")),
            ("invalid_scope", lambda lab, client: None),
        )
        for error, setup in cases:
            with self.subTest(error=error):
                from helpers import build

                lab = build()
                try:
                    client = lab.clients["handheld-north"]
                    setup(lab, client)
                    start = lab.clock.now()
                    sent = len(lab.transport.sent)
                    scope = "excursions.read extra" if error == "invalid_scope" else None
                    with self.assertRaises(TokenEndpointError) as caught:
                        client.refresh(scope)
                    self.assertEqual(caught.exception.error, error)
                    self.assertEqual(len(lab.transport.sent), sent + 1)
                    self.assertEqual(lab.clock.now(), start)
                    if error == "invalid_grant":
                        self.assertEqual(client.status, "reauth_required")
                    else:
                        self.assertEqual(client.status, "active")
                finally:
                    lab.close()
        self.assertNotIn("temporarily_unavailable", TERMINAL_ERRORS)

    def test_scope_can_narrow_the_access_token_only(self) -> None:
        client = self.north()
        response = self.lab.auth.handle(refresh_request(client, scope="excursions.read"))
        self.assertEqual(response.status, 200)
        body = response.json()
        self.assertEqual(body["scope"], "excursions.read")
        self.assertEqual(body["token_type"], "Bearer")
        payload = read_access(body["access_token"])
        self.assertEqual(payload["scope"], "excursions.read")
        self.assertEqual(self.family()["scope"], "excursions.read orders.write")
        self.assertEqual(self.family()["active_generation"], 2)
        denied = self.lab.resources["lot-ledger"].handle(
            order_request(body["access_token"], {"qty": 1, "sku": "crate-ice"}, "narrow-key")
        )
        self.assertEqual(denied.status, 403)
        self.assertEqual(self.lab.store.idempotency_count(), 0)
        allowed = self.lab.resources["lot-ledger"].handle(
            Request(
                "GET",
                "/lot-ledger/entries",
                {"authorization": "Bearer " + body["access_token"]},
            )
        )
        self.assertEqual(allowed.status, 200)

        client.access_token = body["access_token"]
        client.access_exp = payload["exp"]
        client.refresh_token = body["refresh_token"]
        with self.assertRaises(TokenEndpointError) as caught:
            client.refresh("excursions.read extra")
        self.assertEqual(caught.exception.error, "invalid_scope")
        self.assertEqual(client.status, "active")
        self.assertEqual(self.family()["active_generation"], 2)
        client.refresh(None)
        self.assertEqual(self.family()["active_generation"], 3)

    def test_omitted_scope_keeps_the_grant_and_omits_the_field(self) -> None:
        client = self.north()
        response = self.lab.auth.handle(refresh_request(client))
        body = response.json()
        self.assertNotIn("scope", body)
        payload = read_access(body["access_token"])
        self.assertEqual(payload["scope"], "excursions.read orders.write")
        explicit = client
        explicit.refresh_token = body["refresh_token"]
        same = self.lab.auth.handle(
            refresh_request(explicit, scope="excursions.read orders.write")
        )
        self.assertNotIn("scope", same.json())
        self.assertIn(b"scope=excursions.read+orders.write", refresh_request(
            explicit, scope="excursions.read orders.write"
        ).body)

    def test_inactivity_does_not_rotate_or_record_reuse(self) -> None:
        client = self.north()
        access = client.access_token
        self.lab.clock.advance(ACCESS_LIFETIME)
        denied = self.lab.resources["lot-ledger"].handle(
            Request("GET", "/lot-ledger/entries", {"authorization": "Bearer " + access})
        )
        self.assertEqual(denied.status, 401)
        self.assertEqual(self.family()["status"], "active")
        self.assertEqual(self.family()["active_generation"], 1)
        client.refresh()
        self.assertEqual(self.family()["active_generation"], 2)
        self.assertEqual(self.family()["last_used_at"], self.lab.clock.now())
        self.lab.clock.advance(INACTIVITY_WINDOW + 1)
        stamp = self.lab.clock.now()
        with self.assertRaises(TokenEndpointError) as caught:
            client.refresh()
        self.assertEqual(caught.exception.error, "invalid_grant")
        self.assertEqual(self.lab.clock.now(), stamp)
        self.assertEqual(self.family()["active_generation"], 2)
        self.assertEqual(self.family()["status"], "active")
        self.assertEqual(self.lab.store.reuse_count(client.family_id), 0)
        self.assertNotIn(
            "refresh_reuse_detected",
            [row["event"] for row in self.lab.store.logs],
        )
        self.assertEqual(client.status, "reauth_required")

    def test_exact_inactivity_window_still_refreshes(self) -> None:
        client = self.lab.clients["handheld-south"]
        self.lab.clock.advance(INACTIVITY_WINDOW)
        client.refresh()
        family = self.family("handheld-south")
        self.assertEqual(family["active_generation"], 2)
        self.assertEqual(family["status"], "active")
        self.assertEqual(self.lab.store.reuse_count(client.family_id), 0)

    def test_revoked_refresh_family_leaves_unexpired_access_tokens(self) -> None:
        client = self.north()
        original = client.access_token
        ancestor = client.refresh_token
        rotated = self.lab.auth.handle(refresh_request(client, refresh=ancestor))
        issued = rotated.json()["access_token"]
        replay = self.lab.auth.handle(refresh_request(client, refresh=ancestor))
        self.assertEqual(replay.json()["error"], "invalid_grant")
        self.assertEqual(self.family()["status"], "reauth_required")
        for token in (original, issued):
            response = self.lab.resources["lot-ledger"].handle(
                Request("GET", "/lot-ledger/entries", {"authorization": "Bearer " + token})
            )
            self.assertEqual(response.status, 200)

    def test_public_client_requires_an_explicit_mode(self) -> None:
        with self.assertRaises(RegistrationError):
            self.lab.auth.register_client(
                {
                    "audience": "lot-ledger",
                    "client_id": "bare-public",
                    "client_type": "public",
                    "scope": "excursions.read",
                }
            )
        self.assertIsNone(self.lab.store.get_client("bare-public"))

    def test_sender_constraint_refreshes_without_consuming_the_generation(self) -> None:
        probe = self.lab.clients["probe-bound"]
        held = probe.refresh_token
        previous_access = probe.access_token
        self.lab.clock.advance(10)
        probe.refresh()
        family = self.family("probe-bound")
        self.assertEqual(family["active_generation"], 1)
        self.assertEqual(family["status"], "active")
        self.assertEqual(probe.refresh_token, held)
        self.assertIsNone(self.lab.store.find_refresh(token_hash(held))["consumed_at"])
        self.assertNotEqual(probe.access_token, previous_access)
        self.assertEqual(family["last_used_at"], self.lab.clock.now())
        raw = self.lab.auth.handle(probe._token_request(None))
        self.assertEqual(raw.status, 200)
        self.assertNotIn("refresh_token", raw.json())

        bad = refresh_request(
            probe,
            basic_auth=False,
            extra={
                "proof_ts": str(int(self.lab.clock.now())),
                "sender_key_id": "sender-probe-1",
                "sender_proof": "not-the-proof",
            },
        )
        denied = self.lab.auth.handle(bad)
        self.assertEqual(denied.json()["error"], "invalid_grant")
        self.assertEqual(self.family("probe-bound")["active_generation"], 1)
        self.assertEqual(probe.status, "active")
        probe.refresh()
        self.assertEqual(self.family("probe-bound")["active_generation"], 1)

    def test_sender_proof_tolerance_and_ignored_fields_on_rotation(self) -> None:
        probe = self.lab.clients["probe-bound"]
        from lotcycle.tokens import make_sender_proof, sender_key

        now = int(self.lab.clock.now())
        key = sender_key("sender-probe-1")
        inside = str(now + 300)
        accepted = self.lab.auth.handle(
            refresh_request(
                probe,
                basic_auth=False,
                extra={
                    "proof_ts": inside,
                    "sender_key_id": "sender-probe-1",
                    "sender_proof": make_sender_proof(key, "POST", "/token", inside),
                },
            )
        )
        self.assertEqual(accepted.status, 200)
        outside = str(now + 301)
        rejected = self.lab.auth.handle(
            refresh_request(
                probe,
                basic_auth=False,
                extra={
                    "proof_ts": outside,
                    "sender_key_id": "sender-probe-1",
                    "sender_proof": make_sender_proof(key, "POST", "/token", outside),
                },
            )
        )
        self.assertEqual(rejected.json()["error"], "invalid_grant")
        letters = self.lab.auth.handle(
            refresh_request(
                probe,
                basic_auth=False,
                extra={
                    "proof_ts": "12a",
                    "sender_key_id": "sender-probe-1",
                    "sender_proof": "abcd",
                },
            )
        )
        self.assertEqual(letters.json()["error"], "invalid_grant")
        self.assertEqual(self.family("probe-bound")["active_generation"], 1)

        north = self.north()
        rotated = self.lab.auth.handle(
            refresh_request(
                north,
                extra={"sender_proof": "ignored", "proof_ts": "1", "sender_key_id": "nope"},
            )
        )
        self.assertEqual(rotated.status, 200)
        self.assertEqual(self.family()["active_generation"], 2)

    def test_public_rotation_client_sends_client_id_and_advances(self) -> None:
        kiosk = self.lab.clients["kiosk-public"]
        request = kiosk._token_request(None)
        self.assertNotIn("authorization", request.headers)
        self.assertIn(b"client_id=kiosk-public", request.body)
        kiosk.refresh()
        self.assertEqual(self.family("kiosk-public")["active_generation"], 2)

    def test_multiple_credentials_do_not_touch_the_family(self) -> None:
        client = self.north()
        form_body = refresh_request(client).body + b"&client_id=handheld-north"
        response = self.lab.auth.handle(
            Request(
                "POST",
                "/token",
                {
                    "authorization": request_basic(client),
                    "content-type": "application/x-www-form-urlencoded",
                },
                form_body,
            )
        )
        self.assertEqual(response.json()["error"], "invalid_request")
        self.assertEqual(self.family()["active_generation"], 1)
        self.assertEqual(self.lab.store.reuse_count(client.family_id), 0)

    def test_refresh_tokens_are_hashed_and_absent_from_logs(self) -> None:
        seen = set()
        for _ in range(1000):
            token = new_refresh_token()
            self.assertEqual(len(b64url_decode(token)), 32)
            self.assertEqual(len(token), 43)
            self.assertNotIn("=", token)
            self.assertNotIn(token, seen)
            seen.add(token)
        client = self.north()
        raw = client.refresh_token
        access = client.access_token
        stored = self.lab.store.find_refresh(token_hash(raw))
        self.assertIsNotNone(stored)
        self.assertEqual(stored["token_hash"], token_hash(raw))
        self.assertNotIn(raw, stored["token_hash"])
        self.lab.auth.handle(refresh_request(client, secret="nope"))
        unknown = self.lab.auth.handle(refresh_request(client, refresh="definitely-not-issued"))
        self.assertEqual(unknown.json()["error"], "invalid_grant")
        text = logs_text(self.lab.store)
        self.assertNotIn(raw, text)
        self.assertNotIn(access, text)
        self.assertNotIn("lab-secret-north", text)
        self.assertNotIn("definitely-not-issued", text)
        self.assertIn("token_hash", text)
        with self.assertRaises(ValueError):
            self.lab.store.log("blocked", refresh_token=raw)


def _disallow_refresh(lab, client) -> None:
    lab.store.con.execute(
        "UPDATE clients SET refresh_allowed = 0 WHERE client_id = ?",
        (client.client_id,),
    )


def _force_grant(grant: str):
    def setup(lab, client) -> None:
        def request(scope: str | None) -> Request:
            from urllib.parse import urlencode

            from helpers import basic

            headers = {
                "authorization": basic(client.client_id, client.secret),
                "content-type": "application/x-www-form-urlencoded",
            }
            body = urlencode({"grant_type": grant}).encode("utf-8")
            return Request("POST", "/token", headers, body)

        client._token_request = request

    return setup


def request_basic(client) -> str:
    from helpers import basic

    return basic(client.client_id, client.secret)
