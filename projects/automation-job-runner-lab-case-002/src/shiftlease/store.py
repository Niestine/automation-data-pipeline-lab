"""SQLite WAL queue. One writer, database-clock leases, grant-time fences.

Claim, renew, complete, release, and the applied mark are conditional
updates on id, owner, fence, and a deadline that is still ahead of
``datetime('now')`` inside SQL. Worker wall clocks never extend a lease.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from shiftlease.config import Config
from shiftlease.contract import DRAFT_ID, DRAFT_NOTICE, FINGERPRINT_ID
from shiftlease.errors import BusyError, OpenError, SubmitError, ValidationError
from shiftlease.jitter import jitter_lease_term
from shiftlease.payload import (
    canonical_json,
    fingerprint,
    schedule_key,
    validate_payload,
    validate_schedule,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'leased', 'succeeded', 'dead')),
    owner TEXT,
    fence INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    run_at TEXT NOT NULL,
    last_error TEXT,
    result_json TEXT,
    schedule_id TEXT,
    slot_start TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS effect_intents (
    idempotency_key TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL,
    fence INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'applied')),
    detail TEXT
);
CREATE TABLE IF NOT EXISTS job_events (
    job_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    fence INTEGER NOT NULL,
    owner TEXT,
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs (status, run_at, id);
"""


@dataclass
class SubmitResult:
    outcome: str
    http_class: int
    job_id: int
    status: str
    attempts: int
    idempotency_key: str
    result_json: str | None
    last_error: str | None


@dataclass
class ClaimResult:
    kind: str
    job_id: int | None = None
    idempotency_key: str | None = None
    payload: dict | None = None
    fence: int = 0
    owner: str | None = None
    lease_until: str | None = None
    attempts: int = 0
    seq: int = 0
    max_attempts: int = 0
    grant_seconds: int = 0


class QueueStore:
    def __init__(self, conn: sqlite3.Connection, path: Path, config: Config) -> None:
        self.conn = conn
        self.path = Path(path)
        self.config = config
        self._lock = threading.Lock()
        self._open_tx = False

    @classmethod
    def open(
        cls,
        path: str | Path,
        config: Config | None = None,
        *,
        autocheckpoint: int = 1000,
    ) -> QueueStore:
        cfg = config if config is not None else Config()
        db_path = Path(path)
        if db_path.parent and not db_path.parent.exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            db_path,
            isolation_level=None,
            timeout=max(cfg.busy_timeout_ms, 0) / 1000,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.close()
            raise OpenError(f"journal_mode stayed {mode}")
        conn.execute(f"PRAGMA synchronous={cfg.synchronous}")
        conn.execute(f"PRAGMA busy_timeout={int(cfg.busy_timeout_ms)}")
        conn.execute(f"PRAGMA wal_autocheckpoint={int(autocheckpoint)}")
        store = cls(conn, db_path, cfg)
        store._ensure_schema()
        store._reconcile_guard()
        return store

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @property
    def guard_path(self) -> Path:
        return Path(str(self.path) + ".leaseguard")

    def submit(
        self,
        key: str | None,
        payload: object,
        *,
        max_attempts: int | None = None,
        run_at: str | None = None,
        schedule_id: str | None = None,
        slot_start: str | None = None,
    ) -> SubmitResult:
        if key is None or not isinstance(key, str) or not key.strip():
            raise SubmitError(
                "missing_idempotency_key",
                "submit requires an idempotency key; a UUID is recommended",
                400,
            )
        if len(key) > 200 or any(ord(ch) < 32 for ch in key):
            raise ValidationError("idempotency key must be 1 to 200 visible characters")
        body = validate_payload(payload)
        if schedule_id is not None or slot_start is not None:
            if schedule_id is None or slot_start is None:
                raise ValidationError("schedule_id and slot_start are set together")
            validate_schedule(schedule_id, slot_start)
        attempts_cap = self.config.max_attempts if max_attempts is None else max_attempts
        if not isinstance(attempts_cap, int) or isinstance(attempts_cap, bool) or attempts_cap < 1:
            raise ValidationError("max_attempts must be an integer >= 1")
        digest = fingerprint(body)
        encoded = canonical_json(body)
        with self._lock:
            self._begin()
            try:
                existing = self.conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    result = self._replay_or_conflict(existing, digest)
                    self.conn.rollback()
                    return result
                when = run_at if run_at is not None else self._scalar(self._now_sql())
                if run_at is not None:
                    self._require_clock_text(run_at)
                self.conn.execute(
                    f"""
                    INSERT INTO jobs (
                        idempotency_key, payload_fingerprint, payload, status, owner, fence,
                        lease_until, attempts, max_attempts, run_at, last_error, result_json,
                        schedule_id, slot_start, created_at, finished_at
                    ) VALUES (?, ?, ?, 'queued', NULL, 0, NULL, 0, ?, ?, NULL, NULL, ?, ?, {self._now_sql()}, NULL)
                    """,
                    (key, digest, encoded, attempts_cap, when, schedule_id, slot_start),
                )
                job_id = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                epoch = self._bump_epoch()
                self.conn.commit()
            except Exception:
                self._rollback_quietly()
                raise
            self._write_guard(epoch)
            assert row is not None
            return self._submit_from_row(row, "created", 201)

    def enqueue_slot(
        self,
        schedule_id: str,
        slot_start: str,
        payload: object,
        *,
        max_attempts: int | None = None,
    ) -> SubmitResult:
        key = schedule_key(schedule_id, slot_start)
        return self.submit(
            key,
            payload,
            schedule_id=schedule_id,
            slot_start=slot_start,
            max_attempts=max_attempts,
        )

    def claim(self, owner: str, rng, *, commit: bool = True) -> ClaimResult:
        self._check_owner(owner)
        with self._lock:
            self._begin()
            try:
                if self._quarantine_blocks():
                    self.conn.rollback()
                    return ClaimResult(kind="quarantine", owner=owner)
                grant = jitter_lease_term(
                    rng,
                    self.config.lease_term_seconds,
                    self.config.lease_jitter_seconds,
                )
                deadline = self._plus_sql(grant)
                row = self.conn.execute(
                    f"""
                    UPDATE jobs
                    SET owner=?,
                        fence=fence+1,
                        lease_until={deadline},
                        attempts=attempts+1,
                        status='leased'
                    WHERE id = (
                        SELECT id FROM jobs
                        WHERE (status='queued' OR (status='leased' AND lease_until <= {self._now_sql()}))
                          AND run_at <= {self._now_sql()}
                          AND attempts < max_attempts
                        ORDER BY run_at, id
                        LIMIT 1
                    )
                    RETURNING id, idempotency_key, payload, fence, owner, lease_until, attempts, max_attempts
                    """,
                    (owner,),
                ).fetchone()
                if row is None:
                    self.conn.rollback()
                    return ClaimResult(kind="empty", owner=owner)
                self._append_event(int(row["id"]), int(row["fence"]), owner, "claim", None)
                seq = self._max_seq(int(row["id"]))
                self._note_max_term(grant)
                if not commit:
                    self._open_tx = True
                    return self._claim_from_row(row, seq, grant)
                epoch = self._bump_epoch()
                self.conn.commit()
            except Exception:
                self._rollback_quietly()
                raise
            self._write_guard(epoch)
            return self._claim_from_row(row, seq, grant)

    def rollback(self) -> None:
        with self._lock:
            self._rollback_quietly()

    def renew(self, job_id: int, owner: str, fence: int) -> bool:
        return self._conditional(
            f"""
            UPDATE jobs
            SET lease_until={self._plus_sql(self.config.lease_term_seconds)}
            WHERE id=? AND owner=? AND fence=? AND status='leased'
              AND lease_until > {self._now_sql()}
            """,
            (job_id, owner, fence),
            event=None,
        )

    def release(self, job_id: int, owner: str, fence: int) -> bool:
        """Visibility timeout 0: the row is queued again and the attempt stays counted."""
        return self._conditional(
            f"""
            UPDATE jobs
            SET status='queued', lease_until={self._now_sql()}
            WHERE id=? AND owner=? AND fence=? AND status='leased'
              AND lease_until > {self._now_sql()}
            """,
            (job_id, owner, fence),
            event=("release", None),
        )

    def holder_trusts(self, job_id: int, owner: str, fence: int) -> bool:
        """Gray allowance: the holder stops before the stored deadline by the uncertainty."""
        allowance = int(self.config.clock_uncertainty_seconds)
        with self._lock:
            row = self.conn.execute(
                f"""
                SELECT 1 FROM jobs
                WHERE id=? AND owner=? AND fence=? AND status='leased'
                  AND lease_until > datetime({self._now_sql()}, printf('+%d seconds', ?))
                """,
                (job_id, owner, fence, allowance),
            ).fetchone()
        return row is not None

    def ensure_intent(self, job_id: int, owner: str, fence: int, effect_key: str) -> dict | None:
        with self._lock:
            self._begin()
            try:
                live = self.conn.execute(
                    f"""
                    SELECT id FROM jobs
                    WHERE id=? AND owner=? AND fence=? AND status='leased'
                      AND lease_until > {self._now_sql()}
                    """,
                    (job_id, owner, fence),
                ).fetchone()
                if live is None:
                    self.conn.rollback()
                    return None
                intent = self.conn.execute(
                    "SELECT * FROM effect_intents WHERE idempotency_key=?",
                    (effect_key,),
                ).fetchone()
                inserted = False
                if intent is None:
                    try:
                        self.conn.execute(
                            """
                            INSERT INTO effect_intents (idempotency_key, job_id, fence, state, detail)
                            VALUES (?, ?, ?, 'pending', NULL)
                            """,
                            (effect_key, job_id, fence),
                        )
                        inserted = True
                    except sqlite3.IntegrityError:
                        self.conn.rollback()
                        raced = self.conn.execute(
                            "SELECT * FROM effect_intents WHERE idempotency_key=?",
                            (effect_key,),
                        ).fetchone()
                        return dict(raced) if raced is not None else None
                    intent = self.conn.execute(
                        "SELECT * FROM effect_intents WHERE idempotency_key=?",
                        (effect_key,),
                    ).fetchone()
                if inserted:
                    epoch = self._bump_epoch()
                    self.conn.commit()
                else:
                    epoch = None
                    self.conn.rollback()
            except Exception:
                self._rollback_quietly()
                raise
            if epoch is not None:
                self._write_guard(epoch)
            return dict(intent) if intent is not None else None

    def record_error(self, job_id: int, owner: str, fence: int, message: str) -> bool:
        return self._conditional(
            f"""
            UPDATE jobs SET last_error=?
            WHERE id=? AND owner=? AND fence=? AND status='leased'
              AND lease_until > {self._now_sql()}
            """,
            (message, job_id, owner, fence),
            event=None,
        )

    def mark_applied(self, job_id: int, owner: str, fence: int, effect_key: str, result: dict) -> bool:
        """Fault-injection split. The normal completion writes applied and succeeded together.

        The applied mark is still fenced: it lands only while this owner and
        fence hold a live lease on the job.
        """
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._begin()
            try:
                cur = self.conn.execute(
                    f"""
                    UPDATE effect_intents
                    SET state='applied', fence=?, detail=?
                    WHERE idempotency_key=? AND job_id=?
                      AND EXISTS (
                        SELECT 1 FROM jobs
                        WHERE id=? AND owner=? AND fence=? AND status='leased'
                          AND lease_until > {self._now_sql()}
                      )
                    """,
                    (fence, encoded, effect_key, job_id, job_id, owner, fence),
                )
                if cur.rowcount != 1:
                    self.conn.rollback()
                    return False
                epoch = self._bump_epoch()
                self.conn.commit()
            except Exception:
                self._rollback_quietly()
                raise
            self._write_guard(epoch)
            return True

    def complete(self, job_id: int, owner: str, fence: int, effect_key: str, result: dict) -> bool:
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._begin()
            try:
                cur = self.conn.execute(
                    f"""
                    UPDATE jobs
                    SET status='succeeded', result_json=?, finished_at={self._now_sql()}, lease_until=NULL
                    WHERE id=? AND owner=? AND fence=? AND status='leased'
                      AND lease_until > {self._now_sql()}
                    """,
                    (encoded, job_id, owner, fence),
                )
                if cur.rowcount != 1:
                    self.conn.rollback()
                    return False
                cur = self.conn.execute(
                    """
                    UPDATE effect_intents
                    SET state='applied', fence=?, detail=?
                    WHERE idempotency_key=? AND job_id=?
                    """,
                    (fence, encoded, effect_key, job_id),
                )
                if cur.rowcount != 1:
                    self.conn.rollback()
                    return False
                self._append_event(job_id, fence, owner, "complete", None)
                epoch = self._bump_epoch()
                self.conn.commit()
            except Exception:
                self._rollback_quietly()
                raise
            self._write_guard(epoch)
            return True

    def sweep_dead(self) -> list[int]:
        with self._lock:
            self._begin()
            try:
                # A leased row past its deadline, or a row released back to
                # queued on its last attempt, can never be claimed again.
                rows = self.conn.execute(
                    f"""
                    SELECT id, fence, owner, last_error FROM jobs
                    WHERE status IN ('queued', 'leased') AND attempts >= max_attempts
                      AND lease_until IS NOT NULL
                      AND lease_until <= {self._now_sql()}
                    ORDER BY id
                    """
                ).fetchall()
                ids: list[int] = []
                for row in rows:
                    self.conn.execute(
                        f"""
                        UPDATE jobs SET status='dead', finished_at={self._now_sql()}
                        WHERE id=? AND status IN ('queued', 'leased')
                        """,
                        (int(row["id"]),),
                    )
                    self._append_event(
                        int(row["id"]),
                        int(row["fence"]),
                        row["owner"],
                        "dead",
                        row["last_error"],
                    )
                    ids.append(int(row["id"]))
                if not ids:
                    self.conn.rollback()
                    return []
                epoch = self._bump_epoch()
                self.conn.commit()
            except Exception:
                self._rollback_quietly()
                raise
            self._write_guard(epoch)
            return ids

    def preview_next(self) -> dict | None:
        from shiftlease.payload import render_csv

        with self._lock:
            self.conn.execute("BEGIN")
            try:
                if self._quarantine_blocks():
                    self.conn.rollback()
                    return {"kind": "quarantine"}
                row = self.conn.execute(
                    f"""
                    SELECT id, idempotency_key, payload, attempts, max_attempts
                    FROM jobs
                    WHERE (status='queued' OR (status='leased' AND lease_until <= {self._now_sql()}))
                      AND run_at <= {self._now_sql()}
                      AND attempts < max_attempts
                    ORDER BY run_at, id
                    LIMIT 1
                    """
                ).fetchone()
                self.conn.rollback()
            except Exception:
                self._rollback_quietly()
                raise
        if row is None:
            return None
        payload = json.loads(row["payload"])
        csv_text = render_csv(row["idempotency_key"], payload).decode("utf-8")
        return {
            "kind": "preview",
            "job_id": int(row["id"]),
            "idempotency_key": row["idempotency_key"],
            "attempts": int(row["attempts"]),
            "csv": csv_text,
        }

    def status(self) -> dict:
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                counts = self._count_rows()
                quarantine = self._meta("recovery_quarantine_until")
                now = self._scalar(self._now_sql())
                self.conn.rollback()
            except Exception:
                self._rollback_quietly()
                raise
        return {
            "counts": counts,
            "now": now,
            "quarantine_until": quarantine or None,
            "retention_seconds": self.config.retention_seconds,
            "fingerprint": FINGERPRINT_ID,
            "idempotency_draft": DRAFT_ID,
            "draft_notice": DRAFT_NOTICE,
            "synchronous": self.config.synchronous,
            "lease_term_seconds": self.config.lease_term_seconds,
            "clock_uncertainty_seconds": self.config.clock_uncertainty_seconds,
        }

    def counts(self) -> dict[str, int]:
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                counts = self._count_rows()
                self.conn.rollback()
            except Exception:
                self._rollback_quietly()
                raise
        return counts

    def job(self, job_id: int) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row is not None else None

    def job_by_key(self, key: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key=?",
                (key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def events(self, job_id: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY seq",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def intent(self, effect_key: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM effect_intents WHERE idempotency_key=?",
                (effect_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def owned_leases(self, owner: str) -> list[ClaimResult]:
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT id, idempotency_key, payload, fence, owner, lease_until, attempts, max_attempts
                FROM jobs
                WHERE owner=? AND status='leased' AND lease_until > {self._now_sql()}
                ORDER BY id
                """,
                (owner,),
            ).fetchall()
            results = []
            for row in rows:
                seq = self._max_seq(int(row["id"]))
                results.append(self._claim_from_row(row, seq, self.config.lease_term_seconds))
        return results

    def shift_clock(self, seconds: int) -> None:
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            raise ValidationError("clock shift must be an integer number of seconds")
        with self._lock:
            self._begin()
            try:
                self._set_meta("clock_shift_seconds", str(int(seconds)))
                epoch = self._bump_epoch()
                self.conn.commit()
            except Exception:
                self._rollback_quietly()
                raise
            self._write_guard(epoch)

    def clock_now(self) -> str:
        with self._lock:
            return self._scalar(self._now_sql())

    def quarantine_active(self) -> bool:
        with self._lock:
            return self._quarantine_blocks()

    def purge_expired(self) -> int:
        with self._lock:
            self._begin()
            try:
                rows = self.conn.execute(
                    f"""
                    SELECT id FROM jobs
                    WHERE status IN ('succeeded', 'dead')
                      AND finished_at IS NOT NULL
                      AND finished_at <= datetime({self._now_sql()}, printf('-%d seconds', ?))
                    ORDER BY id
                    """,
                    (int(self.config.retention_seconds),),
                ).fetchall()
                ids = [int(row["id"]) for row in rows]
                for job_id in ids:
                    self.conn.execute("DELETE FROM job_events WHERE job_id=?", (job_id,))
                    self.conn.execute("DELETE FROM effect_intents WHERE job_id=?", (job_id,))
                    self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                if not ids:
                    self.conn.rollback()
                    return 0
                epoch = self._bump_epoch()
                self.conn.commit()
            except Exception:
                self._rollback_quietly()
                raise
            self._write_guard(epoch)
            return len(ids)

    def checkpoint(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _ensure_schema(self) -> None:
        with self._lock:
            self.conn.executescript(_SCHEMA)
            defaults = {
                "clock_shift_seconds": "0",
                "commit_epoch": "0",
                "max_lease_term_seconds": str(self.config.max_lease_term_seconds),
                "recovery_quarantine_until": "",
                "acknowledged_guard_epoch": "",
            }
            for key, value in defaults.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                    (key, value),
                )
            if not self.guard_path.exists():
                epoch = int(self._meta("commit_epoch") or "0")
                self._write_guard(epoch)

    def _reconcile_guard(self) -> None:
        """Honor committed deadlines. A guard ahead of the database is a lost WAL."""
        with self._lock:
            guard = self._read_guard()
            db_epoch = int(self._meta("commit_epoch") or "0")
            if guard is None:
                self._write_guard(db_epoch)
                return
            if guard["epoch"] < db_epoch:
                self._write_guard(db_epoch)
                return
            if guard["epoch"] == db_epoch:
                return
            acked = self._meta("acknowledged_guard_epoch")
            if acked == str(guard["epoch"]):
                return
            term = max(int(guard["max_lease_term_seconds"]), self.config.max_lease_term_seconds)
            self._begin()
            try:
                until = self._scalar(self._plus_sql(term))
                self._set_meta("recovery_quarantine_until", until)
                self._set_meta("acknowledged_guard_epoch", str(guard["epoch"]))
                self._set_meta("max_lease_term_seconds", str(term))
                epoch = self._bump_epoch()
                self.conn.commit()
            except Exception:
                self._rollback_quietly()
                raise
            # The guard keeps the higher of the two epochs. The acknowledged epoch
            # stored above stops a later open from extending the quarantine for
            # the same loss; the next ordinary commits move the guard forward.
            self._write_guard(max(epoch, guard["epoch"]))

    def _replay_or_conflict(self, existing: sqlite3.Row, digest: str) -> SubmitResult:
        if existing["payload_fingerprint"] != digest:
            raise SubmitError(
                "payload_mismatch",
                "idempotency key was already used with a different payload",
                422,
                job_id=int(existing["id"]),
            )
        if existing["status"] in {"queued", "leased"}:
            return self._submit_from_row(existing, "conflict", 409)
        return self._submit_from_row(existing, "replay", 200)

    def _submit_from_row(self, row: sqlite3.Row, outcome: str, http_class: int) -> SubmitResult:
        return SubmitResult(
            outcome=outcome,
            http_class=http_class,
            job_id=int(row["id"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            idempotency_key=str(row["idempotency_key"]),
            result_json=row["result_json"],
            last_error=row["last_error"],
        )

    def _claim_from_row(self, row: sqlite3.Row, seq: int, grant: int) -> ClaimResult:
        return ClaimResult(
            kind="job",
            job_id=int(row["id"]),
            idempotency_key=str(row["idempotency_key"]),
            payload=json.loads(row["payload"]),
            fence=int(row["fence"]),
            owner=row["owner"],
            lease_until=row["lease_until"],
            attempts=int(row["attempts"]),
            seq=seq,
            max_attempts=int(row["max_attempts"]),
            grant_seconds=grant,
        )

    def _conditional(self, sql: str, params: tuple, event: tuple[str, str | None] | None) -> bool:
        with self._lock:
            self._begin()
            try:
                cur = self.conn.execute(sql, params)
                if cur.rowcount != 1:
                    self.conn.rollback()
                    return False
                if event is not None:
                    job_id = int(params[0])
                    owner = str(params[1])
                    fence = int(params[2])
                    self._append_event(job_id, fence, owner, event[0], event[1])
                epoch = self._bump_epoch()
                self.conn.commit()
            except Exception:
                self._rollback_quietly()
                raise
            self._write_guard(epoch)
            return True

    def _quarantine_blocks(self) -> bool:
        until = self._meta("recovery_quarantine_until")
        if not until:
            return False
        now = self._scalar(self._now_sql())
        return until > now

    def _count_rows(self) -> dict[str, int]:
        counts = {"queued": 0, "leased": 0, "succeeded": 0, "dead": 0}
        for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
            counts[str(row["status"])] = int(row["n"])
        counts["applied_intents"] = int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM effect_intents WHERE state='applied'"
            ).fetchone()["n"]
        )
        counts["pending_intents"] = int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM effect_intents WHERE state='pending'"
            ).fetchone()["n"]
        )
        return counts

    def _append_event(self, job_id: int, fence: int, owner: str | None, kind: str, detail: str | None) -> None:
        self.conn.execute(
            f"""
            INSERT INTO job_events (job_id, seq, fence, owner, kind, at, detail)
            VALUES (
                ?,
                COALESCE((SELECT MAX(seq) + 1 FROM job_events WHERE job_id=?), 1),
                ?, ?, ?, {self._now_sql()}, ?
            )
            """,
            (job_id, job_id, fence, owner, kind, detail),
        )

    def _max_seq(self, job_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM job_events WHERE job_id=?",
            (job_id,),
        ).fetchone()
        return int(row["seq"])

    def _note_max_term(self, grant: int) -> None:
        current = int(self._meta("max_lease_term_seconds") or "0")
        if grant > current:
            self._set_meta("max_lease_term_seconds", str(grant))

    def _bump_epoch(self) -> int:
        current = int(self._meta("commit_epoch") or "0")
        new = current + 1
        self._set_meta("commit_epoch", str(new))
        return new

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def _now_sql(self) -> str:
        return (
            "datetime('now', printf('%+d seconds', "
            "COALESCE((SELECT CAST(value AS INTEGER) FROM meta WHERE key='clock_shift_seconds'), 0)))"
        )

    def _plus_sql(self, seconds: int) -> str:
        return f"datetime({self._now_sql()}, printf('%+d seconds', {int(seconds)}))"

    def _scalar(self, expr: str) -> str:
        return str(self.conn.execute(f"SELECT {expr}").fetchone()[0])

    def _begin(self) -> None:
        if self._open_tx:
            raise BusyError()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                raise BusyError() from exc
            raise

    def _rollback_quietly(self) -> None:
        self._open_tx = False
        try:
            self.conn.rollback()
        except sqlite3.ProgrammingError:
            return
        except sqlite3.OperationalError:
            return

    def _read_guard(self) -> dict | None:
        path = self.guard_path
        if not path.exists():
            return None
        text = ""
        last: Exception | None = None
        for attempt in range(20):
            try:
                text = path.read_text(encoding="utf-8")
                last = None
                break
            except OSError as exc:
                # Another process may be replacing the guard. A sharing
                # violation is not a corrupt file.
                last = exc
                time.sleep(0.002 * (attempt + 1))
        if last is not None:
            raise OpenError(f"lease guard is unreadable: {last}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"epoch": 1 << 30, "max_lease_term_seconds": self.config.max_lease_term_seconds, "corrupt": True}
        if not isinstance(payload, dict):
            return {"epoch": 1 << 30, "max_lease_term_seconds": self.config.max_lease_term_seconds, "corrupt": True}
        try:
            epoch = int(payload["epoch"])
            term = int(payload["max_lease_term_seconds"])
        except (KeyError, TypeError, ValueError):
            return {"epoch": 1 << 30, "max_lease_term_seconds": self.config.max_lease_term_seconds, "corrupt": True}
        return {"epoch": epoch, "max_lease_term_seconds": term}

    def _write_guard(self, epoch: int) -> None:
        term = int(self._meta("max_lease_term_seconds") or self.config.max_lease_term_seconds)
        raw = json.dumps(
            {"epoch": int(epoch), "max_lease_term_seconds": term},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        path = self.guard_path
        # Distinct temp names: several workers replace the same guard, and a
        # shared *.tmp is locked on Windows for the whole write.
        tmp = Path(f"{path}.{os.getpid()}.{time.time_ns()}.tmp")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp, flags)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            delay = 0.002
            for attempt in range(30):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 29:
                        raise
                    time.sleep(delay)
                    delay = min(delay * 2, 0.05)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    def _check_owner(owner: str) -> None:
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 80:
            raise ValidationError("owner must be 1 to 80 characters")
        if any(ord(ch) < 32 for ch in owner):
            raise ValidationError("owner must be visible characters")

    @staticmethod
    def _require_clock_text(value: str) -> None:
        if len(value) != 19 or value[10] != " ":
            raise ValidationError("run_at must be YYYY-MM-DD HH:MM:SS")
