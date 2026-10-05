"""Budgeted incremental crawl over the local fixture.

New URLs go out before refreshes while the page budget still exceeds the
audit floor. The floor is the oldest synced copies. Everything else is
ordered by the configured freshness or age score. The same-host gap is a
fixed measurement interval, including robots fetches and retries.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from urllib.parse import urljoin

from incremental_crawl_lab.backoff import parse_retry_after, retry_delay
from incremental_crawl_lab.canon import (
    canonicalize,
    extract_hrefs,
    is_allowed_host,
    origin_of,
    path_and_query,
    same_origin,
)
from incremental_crawl_lab.client import FixtureClient, Response, conditional_headers
from incremental_crawl_lab.clock import ManualClock
from incremental_crawl_lab.codec import unwrap_encoding
from incremental_crawl_lab.config import Config
from incremental_crawl_lab.errors import OriginTimeout, SimulatedCrash, UnsafeURL, UnsupportedEncoding
from incremental_crawl_lab.fixture import FixtureOrigin
from incremental_crawl_lab.observe import Decision, bind_soft_hashes, interpret
from incremental_crawl_lab.robots import cache_lifetime, is_allowed, parse_robots, select_rules
from incremental_crawl_lab.schedule import SchedItem, score_age, score_freshness, weighted_sample
from incremental_crawl_lab.store import Store

log = logging.getLogger("incremental_crawl_lab")

_REDIRECTS = {301, 302, 303, 307, 308}
_RETRYABLE = {408, 429, 431}


@dataclass
class Exchange:
    outcome: str
    response: Response | None


class Crawler:
    def __init__(
        self,
        config: Config,
        store: Store,
        origin: FixtureOrigin,
        clock: ManualClock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.origin = origin
        self.clock = clock if clock is not None else ManualClock()
        self.client = FixtureClient(origin)
        self.rng = rng if rng is not None else random.Random(config.seed)
        self.crash_point: str | None = None
        self.transmissions = 0
        self.request_starts: list[float] = []
        self.audit_n = 0
        self.audit_done = 0
        self.protected_audits: set[str] = set()
        self._robots_checked = False
        self.first_horizon_start: float | None = None
        self.last_horizon_end: float | None = None
        bind_soft_hashes(config)

    def run(self) -> None:
        self.store.recover_inflight()
        state = self.store.begin_horizon(self.clock.now, self.config)
        if self.first_horizon_start is None:
            self.first_horizon_start = float(state["horizon_start"])
        self.last_horizon_end = float(state["horizon_end"])
        self.audit_n = self.config.audit_slots(int(state["page_budget_left"]))
        self.audit_done = 0
        self._robots_checked = False
        self.protected_audits = set()
        if self.store.count_urls() == 0:
            self.store.admit(
                self.origin.absolute(self.origin.seed_path),
                origin=self.config.origin,
                depth=0,
                now=self.clock.now,
                capacity=self.config.collection_capacity,
                protected=set(),
            )
        self._ensure_robots()
        steps = 0
        while True:
            row = self._select()
            if row is None:
                break
            self._fetch(row)
            steps += 1
            if steps > 10000:
                raise RuntimeError("crawl did not settle")

    def _ensure_robots(self) -> None:
        if self._robots_checked:
            return
        self._robots_checked = True
        catalog = self.config.origin
        now = self.clock.now
        if self.store.robots_fresh(catalog, now):
            return
        url = catalog + "/robots.txt"
        headers = {"User-Agent": self.config.user_agent, "Accept": "text/plain"}
        redirects = 0
        while True:
            outcome = self._transmit(url, headers, mark_url=None, allow_offlist=True)
            now = self.clock.now
            if outcome.outcome == "timeout":
                self.store.note_robots_fetch(url, now, 0)
                self._robots_policy("", now, now, "disallow_all")
                return
            if outcome.outcome != "ok" or outcome.response is None:
                self._robots_policy("", now, now, "disallow_all")
                return
            response = outcome.response
            self.store.note_robots_fetch(url, now, response.status)
            if response.status in _REDIRECTS:
                redirects += 1
                location = _header(response.headers, "Location")
                if redirects > self.config.max_robots_redirects or not location:
                    self._robots_policy("", now, now, "disallow_all")
                    return
                url = urljoin(url, location)
                continue
            if response.status >= 500:
                self._robots_policy("", now, now, "disallow_all")
                return
            if response.status >= 400:
                ttl = cache_lifetime(
                    response.headers, now, self.config.robots_default_ttl_seconds
                )
                self._robots_policy("", now, now + ttl, "allow_all")
                return
            raw = response.body[: self.config.max_robots_bytes]
            text = raw.decode("utf-8", errors="ignore")
            ttl = cache_lifetime(response.headers, now, self.config.robots_default_ttl_seconds)
            self._robots_policy(text, now, now + ttl, "rules")
            return

    def _robots_policy(self, body: str, fetched_at: float, expiry: float, policy: str) -> None:
        self.store.save_robots(
            self.config.origin,
            body=body,
            fetched_at=fetched_at,
            expiry=expiry,
            policy=policy,
            now=self.clock.now,
        )
        log.info("robots policy %s expires at %s", policy, expiry)

    def _path_allowed(self, url: str) -> bool:
        host = self.store.host(self.config.origin)
        if host is None:
            return False
        policy = host["policy"]
        if policy == "allow_all":
            return True
        if policy != "rules":
            return False
        rules = select_rules(
            parse_robots(host["robots_body"] or "", self.config.max_robots_bytes),
            self.config.product_token,
        )
        return is_allowed(path_and_query(url), rules)

    def _select(self) -> dict | None:
        for _ in range(3):
            picked = self._pick()
            if picked is not None:
                return picked
            wait = self._soonest_wait()
            if wait is None:
                return None
            delay = wait - self.clock.now
            if delay <= 0:
                return None
            if delay > self.config.retry_after_defer_seconds:
                return None
            self.clock.sleep(delay)
        return None

    def _candidate_rows(self) -> list[dict]:
        now = self.clock.now
        rows = self.store.list_retries(now) + self.store.list_new(now) + self.store.list_due(now)
        unique: dict[str, dict] = {}
        for row in rows:
            unique[row["url"]] = row
        return list(unique.values())

    def _blocked(self, row: dict) -> bool:
        return self.store.host_deferred(origin_of(row["url"]), self.clock.now)

    def _ready(self, row: dict) -> bool:
        if self._blocked(row):
            return False
        earliest = max(self.store.host_next(origin_of(row["url"])), float(row["not_before"] or 0.0))
        return earliest <= self.clock.now

    def _pick(self) -> dict | None:
        state = self.store.run_state()
        if state is None or int(state["page_budget_left"]) <= 0:
            return None
        remaining = int(state["page_budget_left"])
        now = self.clock.now
        retries = [row for row in self.store.list_retries(now) if self._ready(row)]
        if retries:
            retries.sort(key=lambda row: (row["depth"], row["url"]))
            return retries[0]
        new = [row for row in self.store.list_new(now) if self._ready(row)]
        due = [row for row in self.store.list_due(now) if not self._blocked(row)]
        due.sort(key=lambda row: (row["synced_at"] if row["synced_at"] is not None else 0.0, row["url"]))
        audits_left = max(0, self.audit_n - self.audit_done)
        reserved = due[:audits_left]
        self.protected_audits = {row["url"] for row in reserved}
        ready_due = [row for row in due if self._ready(row)]
        ready_reserved = [row for row in ready_due if row["url"] in self.protected_audits]
        pool = [row for row in ready_due if row["url"] not in self.protected_audits]
        if new and (remaining > audits_left or not ready_due):
            new.sort(key=lambda row: (row["depth"], row["url"]))
            return new[0]
        if remaining <= audits_left:
            if not ready_reserved:
                return None
            self.audit_done += 1
            return ready_reserved[0]
        if pool:
            return self._rank_one(pool, now)
        if ready_reserved:
            self.audit_done += 1
            return ready_reserved[0]
        return None

    def _rank_one(self, pool: list[dict], now: float) -> dict:
        objective = self.config.objective
        if objective == "uniform":
            return self.rng.choice(pool)
        if objective == "proportional":
            weighted = [row for row in pool if float(row["lambda_hat"]) > 0]
            if not weighted:
                return sorted(pool, key=lambda row: row["url"])[0]
            items = [
                SchedItem(row["url"], float(row["lambda_hat"]), float(row["synced_at"] or 0.0), float(row["importance"]))
                for row in weighted
            ]
            chosen = weighted_sample(self.rng, items, 1)
            target = chosen[0].key if chosen else weighted[0]["url"]
            return next(row for row in weighted if row["url"] == target)
        horizon = float(self.config.horizon_seconds)

        def sort_key(row: dict) -> tuple:
            tau = max(0.0, now - float(row["synced_at"] or now))
            weight = float(row["importance"])
            lam = float(row["lambda_hat"])
            if objective == "age":
                score = score_age(lam, tau, weight)
            else:
                score = score_freshness(lam, tau, horizon, weight)
            return (-score, row["url"])

        return sorted(pool, key=sort_key)[0]

    def _soonest_wait(self) -> float | None:
        waits: list[float] = []
        for row in self._candidate_rows():
            if self._blocked(row):
                continue
            if self._ready(row):
                continue
            earliest = max(
                self.store.host_next(origin_of(row["url"])),
                float(row["not_before"] or 0.0),
            )
            if earliest > self.clock.now:
                waits.append(earliest)
        if not waits:
            return None
        return min(waits)

    def _fetch(self, row: dict) -> None:
        url = row["url"]
        fresh = self.store.get(url) or row
        if not self._path_allowed(url):
            self.store.mark_disallowed(url, self.clock.now)
            log.info("robots blocked %s", url)
            return
        state = self.store.run_state()
        if fresh["consecutive_failures"] and state is not None and int(state["retry_budget_left"]) <= 0:
            self.store.park(
                url,
                float(state["horizon_end"]),
                self.clock.now,
                "deferred",
                {"reason": "retry_budget"},
            )
            return
        headers = conditional_headers(fresh, self.config)
        outcome = self._transmit(url, headers, mark_url=url, allow_offlist=False)
        if outcome.outcome == "deferred":
            return
        if outcome.outcome == "timeout":
            self._fail_retry(url, None, "timeout")
            return
        response = outcome.response
        assert response is not None
        self.store.record(url, self.clock.now, "fetch", {"status": response.status})
        log.info("fetched %s -> %s", url, response.status)
        if response.status in _RETRYABLE or response.status >= 500:
            self._fail_retry(url, response, f"status_{response.status}")
            return
        if response.status in _REDIRECTS:
            self._redirect(url, response)
            return
        if response.status == 304:
            decision = interpret(fresh, 304, response.headers, None, self.config)
        elif response.status in (404, 410):
            decision = interpret(fresh, response.status, response.headers, response.body, self.config)
        elif response.status == 200:
            try:
                raw = unwrap_encoding(response.body, _header(response.headers, "Content-Encoding"))
            except (UnsupportedEncoding, OSError, ValueError) as exc:
                self._decode_error(url, str(exc))
                return
            decision = interpret(fresh, 200, response.headers, raw, self.config)
        else:
            self.store.hold(
                url,
                float(state["horizon_end"]) if state else self.clock.now,
                self.clock.now,
                "client_error",
                {"status": response.status},
            )
            return
        self._commit_decision(url, decision)

    def _commit_decision(self, url: str, decision: Decision) -> None:
        state = self.store.run_state()
        assert state is not None
        applied = self.store.apply_decision(
            url,
            decision,
            self.clock.now,
            sample_interval=float(self.config.sample_interval_seconds or self.config.horizon_seconds),
        )
        if not applied:
            return
        if self.crash_point == "post_result":
            raise SimulatedCrash("post_result")
        if decision.kind in ("baseline", "byte_same", "cosmetic_change", "material_change", "byte_change_unclassified", "soft_error", "validator_not_modified"):
            self._discover(url)

    def _discover(self, url: str) -> None:
        row = self.store.get(url)
        if row is None or not row.get("body"):
            return
        body: bytes = row["body"]
        charset = row.get("charset")
        if row.get("charset_unknown") or not charset:
            text = body.decode("latin-1")
        else:
            try:
                text = body.decode(charset)
            except (LookupError, UnicodeError):
                text = body.decode("latin-1")
        for href in extract_hrefs(text):
            absolute = canonicalize(href, url)
            if not absolute:
                continue
            if not same_origin(absolute, self.config.origin) or not is_allowed_host(
                absolute, self.config.allowlist
            ):
                self.store.record(url, self.clock.now, "off_origin", {"href": absolute})
                continue
            self.store.admit(
                absolute,
                origin=self.config.origin,
                depth=int(row["depth"]) + 1,
                now=self.clock.now,
                capacity=self.config.collection_capacity,
                protected=set(self.protected_audits),
            )

    def _redirect(self, url: str, response: Response) -> None:
        state = self.store.run_state()
        assert state is not None
        self.store.hold(
            url,
            float(state["horizon_end"]),
            self.clock.now,
            "redirect",
            {"status": response.status},
        )
        location = _header(response.headers, "Location")
        if not location:
            return
        absolute = canonicalize(urljoin(url, location))
        if not absolute or not same_origin(absolute, self.config.origin) or not is_allowed_host(
            absolute, self.config.allowlist
        ):
            self.store.record(url, self.clock.now, "off_origin", {"href": absolute})
            return
        parent = self.store.get(url)
        depth = int(parent["depth"]) + 1 if parent else 1
        self.store.admit(
            absolute,
            origin=self.config.origin,
            depth=depth,
            now=self.clock.now,
            capacity=self.config.collection_capacity,
            protected=set(self.protected_audits),
        )

    def _decode_error(self, url: str, reason: str) -> None:
        state = self.store.run_state()
        until = float(state["horizon_end"]) if state else self.clock.now
        self.store.hold(url, until, self.clock.now, "decode_error", {"reason": reason})

    def _fail_retry(self, url: str, response: Response | None, reason: str) -> None:
        state = self.store.run_state()
        assert state is not None
        header = None
        if response is not None:
            header = _header(response.headers, "Retry-After")
        parsed = parse_retry_after(header, self.clock.now)
        host = self.store.host(origin_of(url))
        attempt = int(host["retry_attempt"]) + 1 if host else 1
        delay = retry_delay(
            attempt,
            self.rng,
            base=self.config.backoff_base_seconds,
            cap=self.config.backoff_cap_seconds,
            retry_after_seconds=parsed,
        )
        self.store.fail_retry(
            url,
            origin_of(url),
            self.clock.now,
            delay=delay,
            defer_after=self.config.retry_after_defer_seconds,
            horizon_end=float(state["horizon_end"]),
            reason=reason,
        )

    def _transmit(
        self,
        url: str,
        headers: dict[str, str],
        *,
        mark_url: str | None,
        allow_offlist: bool,
    ) -> Exchange:
        if not allow_offlist and not is_allowed_host(url, self.config.allowlist):
            raise UnsafeURL(url)
        host = origin_of(url)
        if self.store.host_deferred(host, self.clock.now):
            return Exchange("deferred", None)
        nxt = self.store.host_next(host)
        if nxt > self.clock.now:
            self.clock.sleep(nxt - self.clock.now)
        started = self.clock.now
        self.store.arm_request(host, mark_url, started, self.config.min_host_interval_seconds)
        if self.crash_point == "pre_request" and mark_url:
            raise SimulatedCrash("pre_request")
        self.request_starts.append(started)
        self.transmissions += 1
        try:
            response = self.client.request(url, headers, started)
        except OriginTimeout:
            return Exchange("timeout", None)
        return Exchange("ok", response)


def _header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None
