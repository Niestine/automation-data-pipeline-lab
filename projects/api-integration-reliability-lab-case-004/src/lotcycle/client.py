"""Grant client: one refresh in flight, then archive checkpoints and orders."""

from __future__ import annotations

import base64
import json
import random
import threading
from urllib.parse import urlencode

from lotcycle.archive import merge_entry
from lotcycle.clock import VirtualClock
from lotcycle.errors import (
    ConnectionClosed,
    CrashBeforeCommit,
    LabError,
    ReauthRequired,
    RefreshBudgetExhausted,
    ResponseDropped,
    TokenEndpointError,
)
from lotcycle.httputil import Request, Response, parse_link
from lotcycle.params import (
    ARCHIVE_STOP,
    INFLIGHT_RETRIES,
    JITTER_BASE,
    JITTER_CAP,
    POST_SEND_BUDGET,
    PRECOMMIT_BUDGET,
    RESOURCE_BUDGET,
    RETRIABLE_STATUS,
)
from lotcycle.retry import classify_token_http, full_jitter
from lotcycle.schema import canonical_json, validate_entry
from lotcycle.store import Store
from lotcycle.tokens import make_sender_proof, sender_key, unverified_payload


class GrantClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_type: str,
        audience: str,
        mode: str,
        refresh_token: str,
        access_token: str,
        access_exp: float,
        family_id: str,
        transport,
        clock: VirtualClock,
        rng: random.Random,
        store: Store,
        secret: str | None = None,
        sender_key_id: str | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_type = client_type
        self.audience = audience
        self.mode = mode
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.access_exp = access_exp
        self.family_id = family_id
        self.transport = transport
        self.clock = clock
        self.rng = rng
        self.store = store
        self.secret = secret
        self.sender_key_id = sender_key_id
        self.status = "active"
        self.reconstruction_incomplete = False
        self.sync_complete = False
        self.decisions: list[str] = []
        self._lock = threading.Lock()

    def ensure_access(self) -> str:
        with self._lock:
            if self.status == "reauth_required":
                raise ReauthRequired(self.client_id)
            if self.access_token and self.access_exp > self.clock.now():
                return self.access_token
            return self._refresh_locked(None)

    def refresh(self, scope: str | None = None) -> str:
        """Refresh even when the access token has not expired."""
        with self._lock:
            if self.status == "reauth_required":
                raise ReauthRequired(self.client_id)
            return self._refresh_locked(scope)

    def _refresh_locked(self, scope: str | None) -> str:
        # The lock is already held. A second caller waits and then reads
        # whatever this attempt stored. Post-send loss is not retried.
        attempt = 0
        post_send_losses = 0
        while attempt < PRECOMMIT_BUDGET:
            request = self._token_request(scope)
            try:
                response = self.transport.exchange(request)
            except ConnectionClosed as exc:
                self.decisions.append("refresh_presend_retry")
                if attempt + 1 >= PRECOMMIT_BUDGET:
                    raise RefreshBudgetExhausted("pre-send") from exc
                self.clock.sleep(full_jitter(self.rng, attempt, JITTER_BASE, JITTER_CAP))
                attempt += 1
                continue
            except ResponseDropped as exc:
                # The server may have committed generation n+1. Sending
                # generation n again would be a replay, so the budget is 0.
                post_send_losses += 1
                if post_send_losses > POST_SEND_BUDGET:
                    self.status = "reauth_required"
                    self.decisions.append("refresh_post_send_stop")
                    raise ReauthRequired(self.client_id) from exc
                continue
            kind, error = _classify(response)
            if kind == "success":
                self._store_success(response)
                self.decisions.append("refresh_ok")
                return self.access_token
            if kind == "retry":
                self.decisions.append("refresh_precommit_retry")
                if attempt + 1 >= PRECOMMIT_BUDGET:
                    raise RefreshBudgetExhausted(f"http-{response.status}")
                self.clock.sleep(full_jitter(self.rng, attempt, JITTER_BASE, JITTER_CAP))
                attempt += 1
                continue
            self.decisions.append(f"refresh_terminal:{error}")
            if error == "invalid_grant":
                self.status = "reauth_required"
            raise TokenEndpointError(error or "invalid_request", response.status)
        raise RefreshBudgetExhausted("budget")

    def _token_request(self, scope: str | None) -> Request:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        headers = {"content-type": "application/x-www-form-urlencoded"}
        if self.client_type == "confidential":
            if not self.secret:
                raise LabError("missing client secret")
            raw = f"{self.client_id}:{self.secret}".encode("utf-8")
            headers["authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            form["client_id"] = self.client_id
        if self.mode == "sender_constraint":
            if not self.sender_key_id:
                raise LabError("missing sender key id")
            timestamp = str(int(self.clock.now()))
            form["proof_ts"] = timestamp
            form["sender_key_id"] = self.sender_key_id
            form["sender_proof"] = make_sender_proof(
                sender_key(self.sender_key_id), "POST", "/token", timestamp
            )
        if scope is not None:
            form["scope"] = scope
        return Request("POST", "/token", headers, urlencode(form).encode("utf-8"))

    def _store_success(self, response: Response) -> None:
        body = response.json()
        access = body["access_token"]
        if not isinstance(access, str):
            raise ValueError("access_token")
        refresh = body.get("refresh_token")
        if refresh is not None:
            if not isinstance(refresh, str) or not refresh:
                raise ValueError("refresh_token")
            # Persist the new refresh token before the old value is dropped.
            self.refresh_token = refresh
        self.access_token = access
        self.access_exp = float(unverified_payload(access)["exp"])
        self.status = "active"

    def post_order(self, order: dict, key: str) -> Response:
        body = canonical_json(order)
        path = f"/{self.audience}/orders"
        header_key = '"' + key + '"'
        http_tries = 0
        refreshes = 0
        conflicts = 0
        safety = 0
        while safety < 12:
            safety += 1
            token = self.ensure_access()
            request = Request(
                "POST",
                path,
                {
                    "authorization": "Bearer " + token,
                    "content-type": "application/json",
                    "idempotency-key": header_key,
                },
                body,
            )
            try:
                response = self.transport.exchange(request)
            except ConnectionClosed:
                http_tries += 1
                self.decisions.append("order_ConnectionClosed")
                if http_tries >= RESOURCE_BUDGET:
                    raise
                self.clock.sleep(full_jitter(self.rng, http_tries - 1, JITTER_BASE, JITTER_CAP))
                continue
            except ResponseDropped:
                http_tries += 1
                self.decisions.append("order_ResponseDropped")
                if http_tries >= RESOURCE_BUDGET:
                    raise
                continue
            if response.status == 401 and refreshes < 1:
                refreshes += 1
                self.access_exp = self.clock.now()
                self.decisions.append("order_refresh_after_401")
                continue
            if response.status == 409 and conflicts < INFLIGHT_RETRIES:
                conflicts += 1
                self.decisions.append("order_409_retry")
                self.clock.sleep(full_jitter(self.rng, conflicts - 1, JITTER_BASE, JITTER_CAP))
                continue
            if response.status in RETRIABLE_STATUS:
                http_tries += 1
                self.decisions.append("order_retry")
                if http_tries >= RESOURCE_BUDGET:
                    return response
                self.clock.sleep(full_jitter(self.rng, http_tries - 1, JITTER_BASE, JITTER_CAP))
                continue
            return response
        raise LabError("order loop")

    def sync(self, *, commit: bool = True, crash_on_archive: bool = False) -> set[str]:
        """Walk prev-archive. The checkpoint commits with the page upsert."""
        self.reconstruction_incomplete = False
        self.sync_complete = False
        response = self._authorized_get(f"/{self.audience}/entries")
        if response.status in ARCHIVE_STOP:
            self.reconstruction_incomplete = True
            return set()
        if response.status != 200:
            raise LabError(f"head {response.status}")
        document = response.json()
        collected = _ids(document)
        if commit:
            self._store_page(document, None, crash=False)
        path = parse_link(response.headers.get("link")).get("prev-archive")
        while path:
            cursor = path.rstrip("/").split("/")[-1]
            if commit and self.store.checkpointed(self.client_id, self.audience, cursor):
                break
            response = self._authorized_get(path)
            if response.status in ARCHIVE_STOP:
                self.reconstruction_incomplete = True
                if commit:
                    return self.store.local_ids(self.client_id, self.audience)
                return collected
            if response.status != 200:
                raise LabError(f"archive {response.status}")
            document = response.json()
            collected |= _ids(document)
            if commit:
                # crash_on_archive raises CrashBeforeCommit inside the first
                # archive transaction, so the upsert and checkpoint roll back.
                self._store_page(document, cursor, crash=crash_on_archive)
            path = parse_link(response.headers.get("link")).get("prev-archive")
        if commit and not self.reconstruction_incomplete:
            self.sync_complete = True
            return self.store.local_ids(self.client_id, self.audience)
        return collected

    def _store_page(self, document: dict, cursor: str | None, crash: bool) -> None:
        doc_updated = float(document["updated"])
        incoming = []
        for entry in document["entries"]:
            checked = validate_entry(entry)
            incoming.append(
                {
                    "celsius": checked["celsius"],
                    "doc_updated": doc_updated,
                    "id": checked["id"],
                    "lot": checked["lot"],
                    "updated": checked["updated"],
                }
            )
        with self.store.transaction():
            for row in incoming:
                self._upsert(row)
            if cursor is not None:
                self.store.con.execute(
                    """
                    INSERT OR REPLACE INTO checkpoints (client_id, audience, cursor, synced_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (self.client_id, self.audience, cursor, self.clock.now()),
                )
            if crash:
                raise CrashBeforeCommit(cursor or "head")

    def _upsert(self, row: dict) -> None:
        current = self.store.con.execute(
            """
            SELECT updated, doc_updated, payload FROM local_entries
            WHERE client_id = ? AND audience = ? AND entry_id = ?
            """,
            (self.client_id, self.audience, row["id"]),
        ).fetchone()
        chosen = row
        if current is not None:
            existing = json.loads(current["payload"])
            existing["updated"] = current["updated"]
            existing["doc_updated"] = current["doc_updated"]
            chosen = merge_entry(existing, row)
            if chosen is existing:
                return
        payload = canonical_json(
            {
                "celsius": chosen["celsius"],
                "id": chosen["id"],
                "lot": chosen["lot"],
                "updated": chosen["updated"],
            }
        )
        self.store.con.execute(
            """
            INSERT INTO local_entries (
                client_id, audience, entry_id, updated, doc_updated, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id, audience, entry_id) DO UPDATE SET
                updated = excluded.updated,
                doc_updated = excluded.doc_updated,
                payload = excluded.payload
            """,
            (
                self.client_id,
                self.audience,
                chosen["id"],
                chosen["updated"],
                chosen["doc_updated"],
                payload,
            ),
        )

    def _authorized_get(self, path: str) -> Response:
        refreshed = False
        while True:
            token = self.ensure_access()
            response = self.transport.exchange(
                Request("GET", path, {"authorization": "Bearer " + token})
            )
            if response.status == 401 and not refreshed:
                refreshed = True
                self.access_exp = self.clock.now()
                self.decisions.append("get_refresh_after_401")
                continue
            return response


def _ids(document: dict) -> set[str]:
    return {entry["id"] for entry in document["entries"]}


def _classify(response: Response) -> tuple[str, str | None]:
    error = None
    try:
        parsed = response.json()
        raw = parsed.get("error")
        if isinstance(raw, str):
            error = raw
    except (json.JSONDecodeError, ValueError):
        error = None
    return classify_token_http(response.status, error), error
