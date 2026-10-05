"""Mock publisher. 2xx completes, 3xx is not followed, 410 disables.

A manual replay does not clear the automatic schedule. Each attempt mints
a new timestamp and signature; the event id stays put.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .horizons import THROTTLE_STATUSES
from .httpmsg import HttpRequest, HttpResponse
from .mac import sign_standard_header, sign_stripe_secrets
from .schedule import bounded_slot_delay, full_jitter_delay, next_slot, parse_retry_after
from .ssrf import PinnedTarget, ScriptedResolver, pin_https


@dataclass
class Delivery:
    event_id: str
    body: bytes
    profile: str
    attempt: int = 0
    failures: int = 0
    next_at: int = 0
    automatic_open: bool = True
    exhausted: bool = False
    delays: list[int] = field(default_factory=list)


class Publisher:
    def __init__(
        self,
        clock,
        rng,
        target: PinnedTarget,
        transport,
        *,
        profile: str,
        secrets: list[bytes] | None = None,
        stripe_secrets: list[str] | None = None,
        jitter_base: int = 8,
        jitter_cap: int = 64,
        max_throttle_retries: int = 5,
    ) -> None:
        if max_throttle_retries < 0:
            raise ValueError("max_throttle_retries must be non-negative")
        self.clock = clock
        self.rng = rng
        self.target = target
        self.transport = transport
        self.profile = profile
        self.secrets = list(secrets or [])
        self.stripe_secrets = list(stripe_secrets or [])
        self.jitter_base = jitter_base
        self.jitter_cap = jitter_cap
        self.max_throttle_retries = max_throttle_retries
        self.disabled = False
        self.rows: dict[str, Delivery] = {}
        self.connections: list[str] = []
        self.hosts: list[str] = []
        self.snis: list[str] = []
        self.locations_ignored: list[str] = []

    @classmethod
    def register(cls, url: str, resolver: ScriptedResolver, **kwargs) -> "Publisher":
        target = pin_https(url, resolver)
        return cls(target=target, **kwargs)

    def enqueue(self, event_id: str, body: bytes) -> None:
        self.rows[event_id] = Delivery(
            event_id=event_id,
            body=body,
            profile=self.profile,
            next_at=self.clock.time(),
        )

    def pump(self) -> None:
        if self.disabled:
            return
        now = self.clock.time()
        for row in list(self.rows.values()):
            if self.disabled:
                return
            if not row.automatic_open or row.next_at > now:
                continue
            self._send(row, manual=False)

    def manual_replay(self, event_ids: list[str]) -> None:
        """Send now. Leave ``next_at`` and ``automatic_open`` as they were.

        Unknown ids raise ``KeyError`` before anything is sent.
        """

        missing = [event_id for event_id in event_ids if event_id not in self.rows]
        if missing:
            raise KeyError(missing[0])
        for event_id in event_ids:
            row = self.rows[event_id]
            saved = (row.attempt, row.failures, row.next_at, row.automatic_open, row.exhausted)
            self._send(row, manual=True)
            row.attempt, row.failures, row.next_at, row.automatic_open, row.exhausted = saved

    def _send(self, row: Delivery, *, manual: bool) -> HttpResponse | None:
        if self.disabled:
            return None
        timestamp = self.clock.time()
        headers = {"content-type": "application/json"}
        if row.profile == "standard":
            headers["webhook-id"] = row.event_id
            headers["webhook-timestamp"] = str(timestamp)
            headers["webhook-signature"] = sign_standard_header(
                self.secrets, row.event_id, timestamp, row.body
            )
        elif row.profile == "stripe":
            headers["stripe-signature"] = sign_stripe_secrets(
                self.stripe_secrets, timestamp, row.body, include_v0=True
            )
        else:
            raise ValueError("publisher signs standard or stripe profiles")
        self.connections.append(self.target.address)
        self.hosts.append(self.target.host)
        self.snis.append(self.target.sni)
        request = HttpRequest(
            method="POST",
            path=self.target.path,
            authority=self.target.host,
            headers=headers,
            body=row.body,
            peer=self.target.address,
            sni=self.target.sni,
        )
        response = self.transport(request)
        self._classify(row, response, manual=manual)
        return response

    def _classify(self, row: Delivery, response: HttpResponse, *, manual: bool) -> None:
        headers = {key.lower(): value for key, value in response.headers.items()}
        location = headers.get("location")
        if response.status // 100 == 3 and location:
            self.locations_ignored.append(location)
        if manual:
            return
        if response.status == 410:
            self.disabled = True
            row.automatic_open = False
            return
        if 200 <= response.status < 300:
            row.automatic_open = False
            return
        retry_after = parse_retry_after(headers.get("retry-after"))
        throttled = response.status in THROTTLE_STATUSES and retry_after is None
        if throttled and row.failures < self.max_throttle_retries:
            # Shared overload: full jitter, without spending a row of the
            # table. Past the budget, throttles fall back to the table.
            delay = full_jitter_delay(
                row.failures, self.rng, base=self.jitter_base, cap=self.jitter_cap
            )
            row.failures += 1
        else:
            slot = next_slot(row.attempt)
            row.attempt += 1
            if slot is None:
                row.automatic_open = False
                row.exhausted = True
                return
            delay = retry_after if retry_after is not None else bounded_slot_delay(slot, self.rng)
        row.delays.append(delay)
        row.next_at = self.clock.time() + delay
