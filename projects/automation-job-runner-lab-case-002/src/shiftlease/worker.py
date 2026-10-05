"""Claim, heartbeat, and one external effect per idempotency key.

Jeopardy is process-local. A failed renewal stops later effect calls and
stops completion. It is not a database status. The row already belongs to
a higher fence, or it is expired and waiting for reclaim.
"""

from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass

from shiftlease.config import Config
from shiftlease.effect import CsvEffect
from shiftlease.errors import BusyError, CrashInjected, EffectConflict, EffectExhausted
from shiftlease.jitter import full_jitter
from shiftlease.logjson import JsonLogger
from shiftlease.store import ClaimResult, QueueStore


@dataclass
class FaultPlan:
    crash_after_intent: bool = False
    crash_after_effect: bool = False
    crash_after_applied: bool = False


class Worker:
    def __init__(
        self,
        store: QueueStore,
        effect: CsvEffect,
        owner: str,
        config: Config,
        rng: random.Random,
        sleeper,
        logger: JsonLogger,
        faults: FaultPlan | None = None,
    ) -> None:
        self.store = store
        self.effect = effect
        self.owner = owner
        self.config = config
        self.rng = rng
        self.sleeper = sleeper
        self.logger = logger
        self.faults = faults if faults is not None else FaultPlan()
        self.jeopardy = False
        self.heartbeats_enabled = True
        self.effects_started = 0
        self.effects_blocked_by_jeopardy = 0
        self.current: ClaimResult | None = None
        self._stop = threading.Event()
        self._beat_thread: threading.Thread | None = None
        self.empty_attempts = 0

    def run_until_idle(self, max_idle: int = 20, spin_limit: int = 100000) -> str:
        self._start_heartbeat()
        try:
            self.resume_held()
            idle = 0
            spins = 0
            while spins < spin_limit:
                spins += 1
                if self.jeopardy:
                    return "jeopardy"
                kind = self.run_once()
                if kind == "job":
                    idle = 0
                    continue
                if kind in {"quarantine", "jeopardy"}:
                    return kind
                # A dead job can keep a pending intent forever, so only live
                # rows count as remaining work.
                counts = self.store.counts()
                if counts["queued"] == 0 and counts["leased"] == 0:
                    idle += 1
                    if idle >= max_idle:
                        return "drained"
                else:
                    idle = 0
            return "spin_limit"
        finally:
            self.stop_heartbeat()

    def run_once(self) -> str:
        if self.jeopardy:
            return "jeopardy"
        self.store.sweep_dead()
        try:
            claimed = self.store.claim(self.owner, self.rng)
        except BusyError:
            delay = full_jitter(
                self.rng,
                self.empty_attempts,
                self.config.jitter_base_seconds,
                self.config.jitter_cap_seconds,
            )
            self._log("busy_sleep", job_id=None, fence=0, seq=0, attempt=self.empty_attempts, delay_seconds=delay)
            self.empty_attempts += 1
            self.sleeper(delay)
            return "busy"
        if claimed.kind == "quarantine":
            self._log("quarantine", job_id=None, fence=0, seq=0)
            return "quarantine"
        if claimed.kind != "job":
            delay = full_jitter(
                self.rng,
                self.empty_attempts,
                self.config.jitter_base_seconds,
                self.config.jitter_cap_seconds,
            )
            self._log("empty_sleep", job_id=None, fence=0, seq=0, attempt=self.empty_attempts, delay_seconds=delay)
            self.empty_attempts += 1
            self.sleeper(delay)
            return "empty"
        self.empty_attempts = 0
        self._log("claim", job_id=claimed.job_id, fence=claimed.fence, seq=claimed.seq, lease_until=claimed.lease_until)
        self.process(claimed)
        return "job"

    def resume_held(self) -> int:
        rows = self.store.owned_leases(self.owner)
        for claim in rows:
            if self.jeopardy:
                break
            self._log("resume", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)
            self.process(claim)
        return len(rows)

    def process(self, claim: ClaimResult) -> None:
        if claim.job_id is None or claim.idempotency_key is None or claim.payload is None:
            return
        if self.jeopardy:
            self._log("skip_jeopardy", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)
            return
        self.current = claim
        # Heartbeats are per claim. A previous job that gave up its lease does
        # not leave the next job without renewals.
        self.heartbeats_enabled = True
        try:
            intent = self.store.ensure_intent(claim.job_id, self.owner, claim.fence, claim.idempotency_key)
            if intent is None:
                self.jeopardy = True
                self._log("lost_fence", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)
                return
            if self.faults.crash_after_intent:
                raise CrashInjected("after_intent")
            if intent["state"] == "applied":
                self._finish(claim, _loads_result(intent.get("detail")))
                return
            if not self.store.holder_trusts(claim.job_id, self.owner, claim.fence):
                self.jeopardy = True
                self._log("trust_expired", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)
                return
            try:
                result = self._perform(claim)
            except EffectConflict as exc:
                self.store.record_error(claim.job_id, self.owner, claim.fence, str(exc))
                self.heartbeats_enabled = False
                self._log(
                    "effect_conflict",
                    job_id=claim.job_id,
                    fence=claim.fence,
                    seq=claim.seq,
                    error=str(exc),
                )
                return
            except EffectExhausted as exc:
                self.store.record_error(claim.job_id, self.owner, claim.fence, str(exc))
                self.heartbeats_enabled = False
                self._log(
                    "effect_exhausted",
                    job_id=claim.job_id,
                    fence=claim.fence,
                    seq=claim.seq,
                    error=str(exc),
                )
                return
            if self.jeopardy:
                self._log("jeopardy_after_effect", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)
                return
            if self.faults.crash_after_effect:
                raise CrashInjected("after_effect")
            if self.faults.crash_after_applied:
                self.store.mark_applied(claim.job_id, self.owner, claim.fence, claim.idempotency_key, result)
                raise CrashInjected("after_applied")
            self._finish(claim, result)
        finally:
            if self.current is claim:
                self.current = None

    def renew_current(self) -> bool:
        claim = self.current
        if claim is None or claim.job_id is None or not self.heartbeats_enabled:
            return False
        ok = self.store.renew(claim.job_id, self.owner, claim.fence)
        if ok:
            self._log("renewed", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)
            return True
        if self.current is not claim:
            # The job finished or was abandoned while this renewal ran.
            return False
        row = self.store.job(claim.job_id)
        if row is not None and row["status"] == "succeeded":
            return False
        self.jeopardy = True
        self._log("renew_failed", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)
        return False

    def preview(self) -> dict | None:
        found = self.store.preview_next()
        self._log(
            "dry_run",
            job_id=None if not found or found.get("kind") == "quarantine" else found.get("job_id"),
            fence=0,
            seq=0,
        )
        return found

    def start_heartbeat(self) -> None:
        self._start_heartbeat()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        thread = self._beat_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.config.heartbeat_seconds + 1)
        self._beat_thread = None

    def _start_heartbeat(self) -> None:
        if self.config.heartbeat_seconds <= 0 or self._beat_thread is not None:
            return
        self._stop.clear()
        self._beat_thread = threading.Thread(target=self._beat_loop, name=f"heartbeat-{self.owner}", daemon=True)
        self._beat_thread.start()

    def _beat_loop(self) -> None:
        while not self._stop.wait(self.config.heartbeat_seconds):
            if self.current is not None and self.heartbeats_enabled and not self.jeopardy:
                self.renew_current()

    def _finish(self, claim: ClaimResult, result: dict) -> None:
        assert claim.job_id is not None and claim.idempotency_key is not None
        if self.jeopardy:
            self._log("skip_complete", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)
            return
        ok = self.store.complete(claim.job_id, self.owner, claim.fence, claim.idempotency_key, result)
        if not ok:
            self.jeopardy = True
            self._log("stale_complete", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)
            return
        self._log("complete", job_id=claim.job_id, fence=claim.fence, seq=claim.seq)

    def _perform(self, claim: ClaimResult) -> dict:
        assert claim.job_id is not None and claim.idempotency_key is not None and claim.payload is not None
        attempt = 0
        while True:
            if self.jeopardy:
                self.effects_blocked_by_jeopardy += 1
                raise EffectExhausted("jeopardy")
            if not self.store.holder_trusts(claim.job_id, self.owner, claim.fence):
                self.jeopardy = True
                raise EffectExhausted("lease no longer trusted")
            self.effects_started += 1
            try:
                return self.effect.perform(
                    idempotency_key=claim.idempotency_key,
                    payload=claim.payload,
                    fence=claim.fence,
                )
            except EffectConflict:
                raise
            except CrashInjected:
                raise
            except Exception as exc:
                if attempt >= self.config.effect_retry_cap:
                    raise EffectExhausted(str(exc)) from exc
                delay = full_jitter(
                    self.rng,
                    attempt,
                    self.config.jitter_base_seconds,
                    self.config.jitter_cap_seconds,
                )
                self._log(
                    "effect_retry",
                    job_id=claim.job_id,
                    fence=claim.fence,
                    seq=claim.seq,
                    attempt=attempt,
                    delay_seconds=delay,
                )
                self.sleeper(delay)
                attempt += 1

    def _log(self, event: str, *, job_id: int | None, fence: int, seq: int, **fields: object) -> None:
        self.logger.emit(event, job_id=job_id, owner=self.owner, fence=fence, seq=seq, **fields)


def _loads_result(detail: object) -> dict:
    if not isinstance(detail, str) or not detail:
        return {"replayed": True, "bytes": 0, "path": "", "idempotency_key": ""}
    parsed = json.loads(detail)
    if not isinstance(parsed, dict):
        return {"replayed": True, "bytes": 0, "path": "", "idempotency_key": ""}
    return parsed
