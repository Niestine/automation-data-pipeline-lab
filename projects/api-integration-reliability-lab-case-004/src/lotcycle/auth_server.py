"""Token endpoint. Rotation commits in one transaction or it does not commit."""

from __future__ import annotations

import json
import secrets
from typing import Callable

from lotcycle.clock import VirtualClock
from lotcycle.errors import LabError
from lotcycle.httputil import Request, Response, parse_form
from lotcycle.params import (
    ACCESS_LIFETIME,
    INACTIVITY_WINDOW,
    ISSUER,
    MAC_KEY,
    PROOF_TOLERANCE,
    TOKEN_HEADERS,
)
from lotcycle.schema import is_subset, parse_scope
from lotcycle.store import Store
from lotcycle.tokens import (
    b64url,
    issue_access,
    new_refresh_token,
    parse_basic,
    proof_matches,
    secret_matches,
    sender_key,
    token_hash,
)


class RegistrationError(LabError):
    """A client record was refused before any grant existed."""


class _LostRotation(Exception):
    """Another exchange consumed this generation between the check and the commit."""


class AuthServer:
    def __init__(
        self,
        store: Store,
        clock: VirtualClock,
        token_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.store = store
        self.clock = clock
        self.token_bytes = token_bytes
        self.fail_before_commit = 0

    def register_client(self, spec: dict) -> None:
        client_type = spec["client_type"]
        mode = spec.get("mode")
        if client_type == "public" and mode not in ("rotation", "sender_constraint"):
            raise RegistrationError("public client requires rotation or sender_constraint")
        if client_type == "confidential":
            if not spec.get("secret"):
                raise RegistrationError("confidential client requires a secret")
            mode = mode or "rotation"
        if mode not in ("rotation", "sender_constraint"):
            raise RegistrationError("mode")
        row = dict(spec)
        row["mode"] = mode
        if mode == "sender_constraint" and not row.get("sender_key_id"):
            row["sender_key_id"] = "sender-" + spec["client_id"]
        self.store.insert_client(row)

    def issue_family(self, client_id: str) -> dict:
        """Seed one grant. Returns the raw refresh token and the access token."""
        client = self.store.get_client(client_id)
        if client is None:
            raise RegistrationError(client_id)
        refresh = new_refresh_token(self.token_bytes)
        family_id = "fam-" + client_id
        now = self.clock.now()
        access_scope = client["scope"]
        access = self._access_token(client, family_id, access_scope, now + ACCESS_LIFETIME)
        with self.store.transaction():
            self.store.insert_family(
                {
                    "audience": client["audience"],
                    "client_id": client_id,
                    "family_id": family_id,
                    "generation": 1,
                    "last_used_at": now,
                    "mode": client["mode"],
                    "scope": client["scope"],
                }
            )
            self.store.insert_refresh(token_hash(refresh), family_id, 1)
            self.store.insert_access_row(
                {
                    "audience": client["audience"],
                    "client_id": client_id,
                    "expires_at": now + ACCESS_LIFETIME,
                    "family_id": family_id,
                    "issuer": ISSUER,
                    "scope": access_scope,
                    "token_hash": token_hash(access),
                }
            )
        return {
            "access_token": access,
            "audience": client["audience"],
            "expires_at": now + ACCESS_LIFETIME,
            "family_id": family_id,
            "refresh_token": refresh,
            "scope": client["scope"],
        }

    def handle(self, request: Request) -> Response:
        if request.method != "POST" or request.path != "/token":
            return self._error(400, "invalid_request")
        form = parse_form(request.body)
        if form is None:
            return self._error(400, "invalid_request")
        grant = form.get("grant_type")
        if grant is None or grant == "":
            return self._error(400, "invalid_request")
        if grant != "refresh_token":
            self.store.log("token_error", error="unsupported_grant_type", grant_type=grant)
            return self._error(400, "unsupported_grant_type")
        client, failure = self._authenticate(request, form)
        if failure is not None:
            return failure
        assert client is not None
        if not client["refresh_allowed"]:
            self.store.log(
                "token_error",
                client_id=client["client_id"],
                error="unauthorized_client",
            )
            return self._error(400, "unauthorized_client")
        raw_refresh = form.get("refresh_token")
        if not raw_refresh:
            return self._error(400, "invalid_request")
        presented_hash = token_hash(raw_refresh)
        row = self.store.find_refresh(presented_hash)
        if row is None:
            self.store.log("token_error", error="invalid_grant", token_hash=presented_hash)
            return self._error(400, "invalid_grant")
        family = self.store.get_family(row["family_id"])
        if family is None or client["client_id"] != family["client_id"]:
            self.store.log(
                "token_error",
                error="invalid_grant",
                family_id=None if family is None else family["family_id"],
                presenter=client["client_id"],
                token_hash=presented_hash,
            )
            return self._error(400, "invalid_grant")
        if family["status"] != "active":
            self.store.log(
                "token_error",
                error="invalid_grant",
                family_id=family["family_id"],
                presenter=client["client_id"],
            )
            return self._error(400, "invalid_grant")
        if family["mode"] == "sender_constraint":
            if not self._proof_ok(form, family):
                self.store.log(
                    "token_error",
                    error="invalid_grant",
                    family_id=family["family_id"],
                    presenter=client["client_id"],
                    reason="sender_proof",
                )
                return self._error(400, "invalid_grant")
        if self.clock.now() - float(family["last_used_at"]) > INACTIVITY_WINDOW:
            self.store.log(
                "refresh_inactive",
                family_id=family["family_id"],
                presenter=client["client_id"],
            )
            return self._error(400, "invalid_grant")
        requested = parse_scope(form.get("scope") if "scope" in form else None)
        if requested == ():
            self.store.log(
                "token_error",
                error="invalid_scope",
                family_id=family["family_id"],
            )
            return self._error(400, "invalid_scope")
        if requested is not None and not is_subset(requested, family["scope"]):
            self.store.log(
                "token_error",
                error="invalid_scope",
                family_id=family["family_id"],
            )
            return self._error(400, "invalid_scope")
        access_scope = family["scope"] if requested is None else " ".join(requested)
        if row["generation"] != family["active_generation"] or row["consumed_at"] is not None:
            self._revoke_replay(family, int(row["generation"]), client["client_id"])
            return self._error(400, "invalid_grant")
        if self.fail_before_commit > 0:
            self.fail_before_commit -= 1
            self.store.log(
                "token_rollback",
                family_id=family["family_id"],
                generation=int(row["generation"]),
            )
            return Response(500, dict(TOKEN_HEADERS), b'{"error":"server_error"}')
        if family["mode"] == "sender_constraint":
            body = self._issue_without_rotation(family, client, access_scope)
        else:
            try:
                body = self._rotate(family, row, client, access_scope)
            except _LostRotation:
                # A concurrent exchange of the same generation committed first.
                # This presentation is now a replay of a consumed generation.
                self._revoke_replay(family, int(row["generation"]), client["client_id"])
                return self._error(400, "invalid_grant")
        return Response(200, dict(TOKEN_HEADERS), body)

    def _authenticate(self, request: Request, form: dict[str, str]):
        basic = request.headers.get("authorization")
        body_id = form.get("client_id")
        if basic and "client_id" in form:
            self.store.log("token_error", error="invalid_request", reason="multiple_credentials")
            return None, self._error(400, "invalid_request")
        if basic:
            try:
                client_id, secret = parse_basic(basic)
            except ValueError:
                self.store.log("token_error", error="invalid_client")
                return None, self._invalid_client()
            client = self.store.get_client(client_id)
            if (
                client is None
                or client["client_type"] != "confidential"
                or not secret_matches(secret, client["secret"] or "")
            ):
                self.store.log("token_error", error="invalid_client", client_id=client_id)
                return None, self._invalid_client()
            return client, None
        if not body_id:
            return None, self._error(400, "invalid_request")
        client = self.store.get_client(body_id)
        if client is None or client["client_type"] != "public":
            self.store.log("token_error", error="invalid_client", client_id=body_id)
            return None, self._invalid_client()
        return client, None

    def _proof_ok(self, form: dict[str, str], family) -> bool:
        presented = form.get("sender_proof")
        timestamp = form.get("proof_ts")
        key_id = form.get("sender_key_id")
        if not presented or not timestamp or not key_id:
            return False
        if key_id != family_sender_key_id(self.store, family["client_id"]):
            return False
        if not timestamp.isdigit():
            return False
        if abs(self.clock.now() - int(timestamp)) > PROOF_TOLERANCE:
            return False
        return proof_matches(sender_key(key_id), "POST", "/token", timestamp, presented)

    def _revoke_replay(self, family, generation: int, presenter: str) -> None:
        now = self.clock.now()
        with self.store.transaction():
            revoked = self.store.con.execute(
                """
                UPDATE families
                SET status = 'reauth_required', active_generation = NULL
                WHERE family_id = ? AND status = 'active'
                """,
                (family["family_id"],),
            )
            if revoked.rowcount != 1:
                # Already revoked by a concurrent presentation; one reuse row only.
                return
            self.store.con.execute(
                """
                UPDATE refresh_tokens SET consumed_at = ?
                WHERE family_id = ? AND consumed_at IS NULL
                """,
                (now, family["family_id"]),
            )
            self.store.con.execute(
                """
                INSERT INTO reuse_log (family_id, generation, presenter, at)
                VALUES (?, ?, ?, ?)
                """,
                (family["family_id"], generation, presenter, now),
            )
        self.store.log(
            "refresh_reuse_detected",
            family_id=family["family_id"],
            generation=generation,
            presenter=presenter,
        )

    def _rotate(self, family, row, client, access_scope: str) -> bytes:
        refresh = new_refresh_token(self.token_bytes)
        generation = int(row["generation"]) + 1
        now = self.clock.now()
        expires_at = now + ACCESS_LIFETIME
        access = self._access_token(client, family["family_id"], access_scope, expires_at)
        with self.store.transaction():
            updated = self.store.con.execute(
                """
                UPDATE refresh_tokens SET consumed_at = ?
                WHERE token_hash = ? AND consumed_at IS NULL
                """,
                (now, row["token_hash"]),
            )
            advanced = self.store.con.execute(
                """
                UPDATE families
                SET active_generation = ?, last_used_at = ?
                WHERE family_id = ? AND status = 'active' AND active_generation = ?
                """,
                (generation, now, family["family_id"], int(row["generation"])),
            )
            if updated.rowcount != 1 or advanced.rowcount != 1:
                raise _LostRotation(family["family_id"])
            self.store.insert_refresh(token_hash(refresh), family["family_id"], generation)
            self.store.insert_access_row(
                {
                    "audience": family["audience"],
                    "client_id": client["client_id"],
                    "expires_at": expires_at,
                    "family_id": family["family_id"],
                    "issuer": ISSUER,
                    "scope": access_scope,
                    "token_hash": token_hash(access),
                }
            )
        self.store.log(
            "refresh_rotated",
            family_id=family["family_id"],
            generation=generation,
        )
        return _token_body(access, expires_at - now, refresh, access_scope, family["scope"])

    def _issue_without_rotation(self, family, client, access_scope: str) -> bytes:
        now = self.clock.now()
        expires_at = now + ACCESS_LIFETIME
        access = self._access_token(client, family["family_id"], access_scope, expires_at)
        with self.store.transaction():
            self.store.con.execute(
                "UPDATE families SET last_used_at = ? WHERE family_id = ?",
                (now, family["family_id"]),
            )
            self.store.insert_access_row(
                {
                    "audience": family["audience"],
                    "client_id": client["client_id"],
                    "expires_at": expires_at,
                    "family_id": family["family_id"],
                    "issuer": ISSUER,
                    "scope": access_scope,
                    "token_hash": token_hash(access),
                }
            )
        self.store.log(
            "refresh_sender_constrained",
            family_id=family["family_id"],
            generation=int(family["active_generation"]),
        )
        return _token_body(access, expires_at - now, None, access_scope, family["scope"])

    def _access_token(self, client, family_id: str, scope: str, expires_at: float) -> str:
        # jti keeps two issuances at the same virtual second from sharing a hash.
        return issue_access(
            {
                "aud": client["audience"],
                "client_id": client["client_id"],
                "exp": expires_at,
                "family_id": family_id,
                "iat": self.clock.now(),
                "iss": ISSUER,
                "jti": b64url(self.token_bytes(8)),
                "scope": scope,
            },
            MAC_KEY,
        )

    def _error(self, status: int, error: str) -> Response:
        body = json.dumps({"error": error}).encode("utf-8")
        return Response(status, dict(TOKEN_HEADERS), body)

    def _invalid_client(self) -> Response:
        headers = dict(TOKEN_HEADERS)
        headers["www-authenticate"] = 'Basic realm="token"'
        body = json.dumps({"error": "invalid_client"}).encode("utf-8")
        return Response(401, headers, body)


def family_sender_key_id(store: Store, client_id: str) -> str | None:
    client = store.get_client(client_id)
    if client is None:
        return None
    return client["sender_key_id"]


def _token_body(
    access: str,
    expires_in: float,
    refresh: str | None,
    access_scope: str,
    refresh_scope: str,
) -> bytes:
    payload: dict[str, object] = {
        "access_token": access,
        "expires_in": int(expires_in),
        "token_type": "Bearer",
    }
    if refresh is not None:
        payload["refresh_token"] = refresh
    if access_scope != refresh_scope:
        payload["scope"] = access_scope
    return json.dumps(payload).encode("utf-8")
