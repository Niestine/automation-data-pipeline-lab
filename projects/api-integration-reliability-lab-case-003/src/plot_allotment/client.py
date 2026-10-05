"""Retrying in-process client for the registrar API."""

from __future__ import annotations

import json
import random

from .errors import InvalidArgument
from .httpmsg import Request, Response
from .journal import Journal
from .retry import RetryPolicy, call_with_retry
from .schema import canonical_bytes, format_idempotency_key
from .service import Page, Service


class ExportClient:
    def __init__(
        self,
        service: Service,
        caller: str,
        sleeper,
        rng: random.Random,
        journal: Journal,
        policy: RetryPolicy | None = None,
    ) -> None:
        self.service = service
        self.caller = caller
        self.sleeper = sleeper
        self.rng = rng
        self.journal = journal
        self.policy = policy or RetryPolicy()
        self.calls = 0

    def send(self, request: Request) -> Response:
        def operation() -> Response:
            self.calls += 1
            return self.service.handle(request)

        return call_with_retry(operation, self.policy, self.rng, self.sleeper, self.journal)

    def list_page(
        self,
        parent: str,
        page_size: int,
        filter_raw: str,
        order: str,
        page_token: str | None,
    ) -> Page:
        query = {"order": order, "page_size": str(page_size)}
        if filter_raw:
            query["filter"] = filter_raw
        if page_token:
            query["page_token"] = page_token
        request = Request(
            method="GET",
            path=f"/v1/{parent}/resources",
            query=query,
            headers={"authorization": f"Bearer {self.caller}"},
        )
        response = self.send(request)
        if response.status != 200:
            raise InvalidArgument(response.body.decode("utf-8", "replace"))
        payload = json.loads(response.body.decode("utf-8"))
        return Page(
            resources=payload["resources"],
            next_token=payload.get("next_page_token") or "",
            snapshot_id=payload["snapshot_id"],
            request_token=page_token or "",
            parent=parent,
        )

    def walk(self, parent: str, page_size: int, filter_raw: str = "", order: str = "sort_key") -> list[dict]:
        """Follow next_page_token until it is empty. A short page does not stop the walk."""
        token = None
        rows: list[dict] = []
        seen: set[str] = set()
        while True:
            page = self.list_page(parent, page_size, filter_raw, order, token)
            rows.extend(page.resources)
            if not page.next_token:
                return rows
            if page.next_token in seen:
                raise RuntimeError("repeated page token")
            seen.add(page.next_token)
            if len(seen) > 10000:
                raise RuntimeError("pagination did not end")
            token = page.next_token

    def upsert(
        self,
        parent: str,
        fields: dict,
        key: str,
        if_match: str | None = None,
    ) -> Response:
        headers = {
            "authorization": f"Bearer {self.caller}",
            "content-type": "application/json",
            "idempotency-key": format_idempotency_key(key),
        }
        if if_match is not None:
            headers["if-match"] = if_match
        request = Request(
            method="POST",
            path=f"/v1/{parent}/resources:upsert",
            headers=headers,
            body=canonical_bytes(fields),
        )
        return self.send(request)
