"""One SQLite database shared by the source API, the ledger, and the worker."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from .errors import CheckpointIOError, CheckpointMismatch, IdempotencyConflict, LeaseDenied
from .journal import Journal
from .schema import IDEMPOTENCY_TTL_SECONDS, canonical_dumps, fingerprint, page_replay_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_rows (
    parent TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    sort_key INTEGER NOT NULL,
    source_version INTEGER NOT NULL,
    plot_code TEXT NOT NULL,
    holder_label TEXT NOT NULL,
    beds INTEGER NOT NULL,
    note TEXT NOT NULL,
    etag TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (parent, resource_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL,
    parent TEXT NOT NULL,
    filter_raw TEXT NOT NULL,
    order_by TEXT NOT NULL,
    bound_sort INTEGER,
    bound_id TEXT,
    opened_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_rows (
    snapshot_id TEXT NOT NULL,
    parent TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    sort_key INTEGER NOT NULL,
    source_version INTEGER NOT NULL,
    plot_code TEXT NOT NULL,
    holder_label TEXT NOT NULL,
    beds INTEGER NOT NULL,
    note TEXT NOT NULL,
    etag TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, resource_id)
);
CREATE TABLE IF NOT EXISTS page_tokens (
    token TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL,
    parent TEXT NOT NULL,
    filter_raw TEXT NOT NULL,
    order_by TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    cursor_sort INTEGER,
    cursor_id TEXT,
    has_cursor INTEGER NOT NULL,
    bound_sort INTEGER,
    bound_id TEXT,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    parent TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    sort_key INTEGER NOT NULL,
    source_version INTEGER NOT NULL,
    plot_code TEXT NOT NULL,
    holder_label TEXT NOT NULL,
    beds INTEGER NOT NULL,
    note TEXT NOT NULL,
    etag TEXT NOT NULL,
    origin_snapshot TEXT NOT NULL,
    PRIMARY KEY (parent, resource_id)
);
CREATE TABLE IF NOT EXISTS idempotency (
    caller_id TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    claim_token TEXT,
    status INTEGER,
    response_body TEXT,
    response_etag TEXT,
    resource_parent TEXT,
    resource_id TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (caller_id, idem_key)
);
CREATE TABLE IF NOT EXISTS webhook_inbox (
    webhook_id TEXT PRIMARY KEY,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    scope TEXT PRIMARY KEY,
    consumed_token TEXT,
    next_token TEXT,
    param_fingerprint TEXT NOT NULL,
    snapshot_id TEXT,
    lease_owner TEXT,
    lease_until REAL,
    applied_token TEXT,
    apply_ahead INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshot_order
    ON snapshot_rows (snapshot_id, parent, sort_key, resource_id);
CREATE INDEX IF NOT EXISTS idx_source_order
    ON source_rows (parent, deleted, sort_key, resource_id);
"""


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class Store:
    def __init__(self, path: str = ":memory:", journal: Journal | None = None) -> None:
        self.journal = journal or Journal()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.idempotency_lookups = 0
        self.executions: list[tuple[str, str]] = []
        self._txn_executions: list[tuple[str, str]] = []
        self.fail_next_checkpoint = False
        self._etag = 0
        self.conn.executescript(_SCHEMA)
        self._etag = self._highest_etag()

    def _highest_etag(self) -> int:
        best = 0
        for table in ("source_rows", "ledger"):
            rows = self.conn.execute(f"SELECT etag FROM {table}").fetchall()
            for row in rows:
                text = str(row["etag"]).strip('"')
                if text.startswith("e-") and text[2:].isdigit():
                    best = max(best, int(text[2:]))
        return best

    def close(self) -> None:
        self.conn.close()

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.commit()
        self.executions.extend(self._txn_executions)
        self._txn_executions.clear()

    def rollback(self) -> None:
        self.conn.rollback()
        self._txn_executions.clear()

    def next_etag(self) -> str:
        self._etag += 1
        return f'"e-{self._etag:04d}"'

    def insert_source(self, parent: str, fields: dict[str, Any], etag: str | None = None) -> str:
        with self.lock:
            self.begin()
            try:
                assigned = etag or self.next_etag()
                self._insert_source(parent, fields, assigned)
                self.commit()
            except Exception:
                self.rollback()
                raise
        return assigned

    def _insert_source(self, parent: str, fields: dict[str, Any], etag: str) -> None:
        self.conn.execute(
            """
            INSERT INTO source_rows (
                parent, resource_id, sort_key, source_version, plot_code,
                holder_label, beds, note, etag, deleted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                parent,
                fields["resource_id"],
                fields["sort_key"],
                fields["source_version"],
                fields["plot_code"],
                fields["holder_label"],
                fields["beds"],
                fields["note"],
                etag,
            ),
        )

    def delete_source(self, parent: str, resource_id: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE source_rows SET deleted = 1 WHERE parent = ? AND resource_id = ?",
                (parent, resource_id),
            )

    def update_source_sort(self, parent: str, resource_id: str, sort_key: int) -> None:
        with self.lock:
            self.conn.execute(
                """
                UPDATE source_rows SET sort_key = ?
                WHERE parent = ? AND resource_id = ? AND deleted = 0
                """,
                (sort_key, parent, resource_id),
            )

    def offset_page(self, parent: str, offset: int, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT * FROM source_rows
                WHERE parent = ? AND deleted = 0
                ORDER BY sort_key ASC, resource_id ASC
                LIMIT ? OFFSET ?
                """,
                (parent, limit, offset),
            ).fetchall()
        return [self._public_source(row) for row in rows]

    def live_keyset_page(
        self,
        parent: str,
        cursor: tuple[int, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """READ COMMITTED keyset with no snapshot bound. The tail can move."""
        with self.lock:
            if cursor is None:
                rows = self.conn.execute(
                    """
                    SELECT * FROM source_rows
                    WHERE parent = ? AND deleted = 0
                    ORDER BY sort_key ASC, resource_id ASC
                    LIMIT ?
                    """,
                    (parent, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """
                    SELECT * FROM source_rows
                    WHERE parent = ? AND deleted = 0
                      AND (sort_key > ? OR (sort_key = ? AND resource_id > ?))
                    ORDER BY sort_key ASC, resource_id ASC
                    LIMIT ?
                    """,
                    (parent, cursor[0], cursor[0], cursor[1], limit),
                ).fetchall()
        return [self._public_source(row) for row in rows]

    def live_ids(self, parent: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT resource_id FROM source_rows
                WHERE parent = ? AND deleted = 0
                ORDER BY sort_key, resource_id
                """,
                (parent,),
            ).fetchall()
        return [row["resource_id"] for row in rows]

    def _public_source(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "beds": row["beds"],
            "etag": row["etag"],
            "holder_label": row["holder_label"],
            "note": row["note"],
            "parent": row["parent"],
            "plot_code": row["plot_code"],
            "resource_id": row["resource_id"],
            "sort_key": row["sort_key"],
            "source_version": row["source_version"],
        }

    def copy_snapshot(
        self,
        snapshot_id: str,
        caller: str,
        parent: str,
        filter_raw: str,
        note_equals: str | None,
        order_by: str,
        opened_at: float,
    ) -> tuple[int | None, str | None]:
        if note_equals is None:
            rows = self.conn.execute(
                """
                SELECT * FROM source_rows
                WHERE parent = ? AND deleted = 0
                ORDER BY sort_key ASC, resource_id ASC
                """,
                (parent,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM source_rows
                WHERE parent = ? AND deleted = 0 AND note = ?
                ORDER BY sort_key ASC, resource_id ASC
                """,
                (parent, note_equals),
            ).fetchall()
        bound_sort = None
        bound_id = None
        for row in rows:
            self.conn.execute(
                """
                INSERT INTO snapshot_rows (
                    snapshot_id, parent, resource_id, sort_key, source_version,
                    plot_code, holder_label, beds, note, etag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    parent,
                    row["resource_id"],
                    row["sort_key"],
                    row["source_version"],
                    row["plot_code"],
                    row["holder_label"],
                    row["beds"],
                    row["note"],
                    row["etag"],
                ),
            )
            bound_sort = row["sort_key"]
            bound_id = row["resource_id"]
        self.conn.execute(
            """
            INSERT INTO snapshots (
                snapshot_id, caller_id, parent, filter_raw, order_by,
                bound_sort, bound_id, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                caller,
                parent,
                filter_raw,
                order_by,
                bound_sort,
                bound_id,
                opened_at,
            ),
        )
        return bound_sort, bound_id

    def snapshot_meta(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        return _row_dict(row)

    def snapshot_page(
        self,
        snapshot_id: str,
        parent: str,
        cursor: tuple[int, str] | None,
        bound: tuple[int, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        if bound is None:
            return []
        if cursor is None:
            rows = self.conn.execute(
                """
                SELECT * FROM snapshot_rows
                WHERE snapshot_id = ? AND parent = ?
                  AND (sort_key < ? OR (sort_key = ? AND resource_id <= ?))
                ORDER BY sort_key ASC, resource_id ASC
                LIMIT ?
                """,
                (snapshot_id, parent, bound[0], bound[0], bound[1], limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM snapshot_rows
                WHERE snapshot_id = ? AND parent = ?
                  AND (sort_key > ? OR (sort_key = ? AND resource_id > ?))
                  AND (sort_key < ? OR (sort_key = ? AND resource_id <= ?))
                ORDER BY sort_key ASC, resource_id ASC
                LIMIT ?
                """,
                (
                    snapshot_id,
                    parent,
                    cursor[0],
                    cursor[0],
                    cursor[1],
                    bound[0],
                    bound[0],
                    bound[1],
                    limit,
                ),
            ).fetchall()
        return [self._public_source(row) for row in rows]

    def snapshot_ids(self, snapshot_id: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT resource_id FROM snapshot_rows
                WHERE snapshot_id = ?
                ORDER BY sort_key, resource_id
                """,
                (snapshot_id,),
            ).fetchall()
        return [row["resource_id"] for row in rows]

    def put_token(self, record: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO page_tokens (
                token, caller_id, parent, filter_raw, order_by, snapshot_id,
                cursor_sort, cursor_id, has_cursor, bound_sort, bound_id, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["token"],
                record["caller_id"],
                record["parent"],
                record["filter_raw"],
                record["order_by"],
                record["snapshot_id"],
                record["cursor_sort"],
                record["cursor_id"],
                1 if record["has_cursor"] else 0,
                record["bound_sort"],
                record["bound_id"],
                record["expires_at"],
            ),
        )

    def get_token(self, token: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM page_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        return _row_dict(row)

    def idem_row(self, caller: str, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM idempotency WHERE caller_id = ? AND idem_key = ?",
                (caller, key),
            ).fetchone()
        return _row_dict(row)

    def prepare_idempotency(
        self, caller: str, key: str, fp: str, now: float
    ) -> dict[str, Any]:
        """Lookup or insert in_progress. Caller holds the transaction."""
        self.idempotency_lookups += 1
        row = self.conn.execute(
            "SELECT * FROM idempotency WHERE caller_id = ? AND idem_key = ?",
            (caller, key),
        ).fetchone()
        if row is not None and now >= float(row["expires_at"]):
            self.conn.execute(
                "DELETE FROM idempotency WHERE caller_id = ? AND idem_key = ?",
                (caller, key),
            )
            self.journal.record("idempotency_expired", "execute_again", caller=caller)
            row = None
        if row is None:
            claim = f"claim-{self._etag + 1}-{self.idempotency_lookups}"
            self.conn.execute(
                """
                INSERT INTO idempotency (
                    caller_id, idem_key, fingerprint, state, claim_token, status,
                    response_body, response_etag, resource_parent, resource_id,
                    created_at, expires_at
                ) VALUES (?, ?, ?, 'in_progress', ?, NULL, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (caller, key, fp, claim, now, now + IDEMPOTENCY_TTL_SECONDS),
            )
            return {"kind": "claim", "claim_token": claim}
        if row["fingerprint"] != fp:
            self.journal.record("fingerprint_422", "stop_payload_mismatch", caller=caller)
            return {"kind": "mismatch"}
        if row["state"] == "in_progress":
            self.journal.record("idempotency_409", "retry_without_changes", caller=caller)
            return {"kind": "in_progress"}
        if row["response_body"] is None:
            self.journal.record(
                "idempotency_pruned", "return_current_resource", caller=caller
            )
            return {
                "kind": "current",
                "resource_parent": row["resource_parent"],
                "resource_id": row["resource_id"],
            }
        self.journal.record("idempotency_replay", "return_stored_body", caller=caller)
        return {
            "kind": "stored",
            "status": int(row["status"]),
            "body": row["response_body"],
            "etag": row["response_etag"],
        }

    def finish_idempotency(
        self,
        caller: str,
        key: str,
        claim_token: str,
        parent: str,
        fields: dict[str, Any],
        if_match: str | None,
        origin_snapshot: str,
        now: float,
    ) -> dict[str, Any]:
        """Apply a fresh upsert or delete the claim on a precondition miss."""
        current = self.conn.execute(
            "SELECT * FROM ledger WHERE parent = ? AND resource_id = ?",
            (parent, fields["resource_id"]),
        ).fetchone()
        current_etag = current["etag"] if current is not None else None
        if if_match is not None and if_match != current_etag:
            self.conn.execute(
                """
                DELETE FROM idempotency
                WHERE caller_id = ? AND idem_key = ? AND claim_token = ?
                """,
                (caller, key, claim_token),
            )
            self.journal.record("precondition_412", "stop_not_a_replay", caller=caller)
            return {"kind": "precondition"}
        etag = self.next_etag()
        self.conn.execute(
            """
            INSERT INTO ledger (
                parent, resource_id, sort_key, source_version, plot_code,
                holder_label, beds, note, etag, origin_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(parent, resource_id) DO UPDATE SET
                sort_key = excluded.sort_key,
                source_version = excluded.source_version,
                plot_code = excluded.plot_code,
                holder_label = excluded.holder_label,
                beds = excluded.beds,
                note = excluded.note,
                etag = excluded.etag,
                origin_snapshot = excluded.origin_snapshot
            """,
            (
                parent,
                fields["resource_id"],
                fields["sort_key"],
                fields["source_version"],
                fields["plot_code"],
                fields["holder_label"],
                fields["beds"],
                fields["note"],
                etag,
                origin_snapshot,
            ),
        )
        resource = self._ledger_public(parent, fields, etag)
        body = canonical_dumps({"resource": resource})
        self.conn.execute(
            """
            UPDATE idempotency
            SET state = 'completed', status = 200, response_body = ?,
                response_etag = ?, resource_parent = ?, resource_id = ?,
                expires_at = ?
            WHERE caller_id = ? AND idem_key = ? AND claim_token = ?
            """,
            (
                body,
                etag,
                parent,
                fields["resource_id"],
                now + IDEMPOTENCY_TTL_SECONDS,
                caller,
                key,
                claim_token,
            ),
        )
        self._txn_executions.append((parent, fields["resource_id"]))
        return {"kind": "created", "status": 200, "body": body, "etag": etag, "resource": resource}

    def release_claim(self, caller: str, key: str, claim_token: str) -> None:
        """Drop an in_progress claim whose handler failed, so a retry is not frozen at 409."""
        with self.lock:
            self.begin()
            try:
                self.conn.execute(
                    """
                    DELETE FROM idempotency
                    WHERE caller_id = ? AND idem_key = ? AND claim_token = ?
                      AND state = 'in_progress'
                    """,
                    (caller, key, claim_token),
                )
                self.commit()
            except Exception:
                self.rollback()
                raise
        self.journal.record("handler_error", "release_claim", caller=caller)

    def completed_upserts_by_resource(self, parent: str) -> dict[str, int]:
        """Completed idempotency records per resource. A re-execution under a new key shows as > 1."""
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT resource_id, COUNT(*) AS n FROM idempotency
                WHERE resource_parent = ? AND state = 'completed'
                GROUP BY resource_id
                """,
                (parent,),
            ).fetchall()
        return {row["resource_id"]: int(row["n"]) for row in rows}

    def upsert_ledger_direct(
        self, parent: str, fields: dict[str, Any], origin: str
    ) -> str:
        """Ledger write for a webhook. Caller holds the transaction."""
        etag = self.next_etag()
        self.conn.execute(
            """
            INSERT INTO ledger (
                parent, resource_id, sort_key, source_version, plot_code,
                holder_label, beds, note, etag, origin_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(parent, resource_id) DO UPDATE SET
                sort_key = excluded.sort_key,
                source_version = excluded.source_version,
                plot_code = excluded.plot_code,
                holder_label = excluded.holder_label,
                beds = excluded.beds,
                note = excluded.note,
                etag = excluded.etag,
                origin_snapshot = excluded.origin_snapshot
            """,
            (
                parent,
                fields["resource_id"],
                fields["sort_key"],
                fields["source_version"],
                fields["plot_code"],
                fields["holder_label"],
                fields["beds"],
                fields["note"],
                etag,
                origin,
            ),
        )
        self._txn_executions.append((parent, fields["resource_id"]))
        return etag

    def _ledger_public(self, parent: str, fields: dict[str, Any], etag: str) -> dict[str, Any]:
        resource = {
            "beds": fields["beds"],
            "etag": etag,
            "holder_label": fields["holder_label"],
            "note": fields["note"],
            "parent": parent,
            "plot_code": fields["plot_code"],
            "resource_id": fields["resource_id"],
            "sort_key": fields["sort_key"],
            "source_version": fields["source_version"],
        }
        if "request_id" in resource:
            raise RuntimeError("request_id must not be stored on the resource")
        return resource

    def ledger_get(self, parent: str, resource_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM ledger WHERE parent = ? AND resource_id = ?",
                (parent, resource_id),
            ).fetchone()
        if row is None:
            return None
        return self._ledger_public(parent, _row_dict(row) or {}, row["etag"])

    def ledger_ids(self, parent: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT resource_id FROM ledger
                WHERE parent = ?
                ORDER BY sort_key, resource_id
                """,
                (parent,),
            ).fetchall()
        return [row["resource_id"] for row in rows]

    def ledger_count(self, parent: str | None = None) -> int:
        with self.lock:
            if parent is None:
                row = self.conn.execute("SELECT COUNT(*) AS n FROM ledger").fetchone()
            else:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM ledger WHERE parent = ?",
                    (parent,),
                ).fetchone()
        return int(row["n"])

    def ledger_columns(self) -> list[str]:
        with self.lock:
            rows = self.conn.execute("PRAGMA table_info(ledger)").fetchall()
        return [row["name"] for row in rows]

    def prune_idempotency_body(self, caller: str, key: str) -> None:
        with self.lock:
            self.conn.execute(
                """
                UPDATE idempotency
                SET response_body = NULL, response_etag = NULL
                WHERE caller_id = ? AND idem_key = ? AND state = 'completed'
                """,
                (caller, key),
            )

    def current_resource_body(self, parent: str, resource_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM ledger WHERE parent = ? AND resource_id = ?",
            (parent, resource_id),
        ).fetchone()
        if row is None:
            return None
        return self._ledger_public(parent, _row_dict(row) or {}, row["etag"])

    def inbox_has(self, webhook_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 AS ok FROM webhook_inbox WHERE webhook_id = ?",
            (webhook_id,),
        ).fetchone()
        return row is not None

    def inbox_ids(self) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT webhook_id FROM webhook_inbox ORDER BY webhook_id"
            ).fetchall()
        return [row["webhook_id"] for row in rows]

    def insert_inbox(self, webhook_id: str, received_at: float) -> None:
        self.conn.execute(
            "INSERT INTO webhook_inbox (webhook_id, received_at) VALUES (?, ?)",
            (webhook_id, received_at),
        )

    def prune_inbox(self, now: float, retention: float) -> int:
        with self.lock:
            cursor = self.conn.execute(
                "DELETE FROM webhook_inbox WHERE received_at <= ?",
                (now - retention,),
            )
        return int(cursor.rowcount)

    def try_acquire(
        self, scope: str, owner: str, now: float, ttl: float, param_fp: str
    ) -> bool:
        until = now + ttl
        with self.lock:
            self.begin()
            try:
                row = self.conn.execute(
                    "SELECT * FROM checkpoints WHERE scope = ?",
                    (scope,),
                ).fetchone()
                if row is None:
                    self.conn.execute(
                        """
                        INSERT INTO checkpoints (
                            scope, consumed_token, next_token, param_fingerprint,
                            snapshot_id, lease_owner, lease_until, applied_token,
                            apply_ahead, done
                        ) VALUES (?, NULL, NULL, ?, NULL, ?, ?, NULL, 0, 0)
                        """,
                        (scope, param_fp, owner, until),
                    )
                    self.commit()
                    return True
                if row["param_fingerprint"] != param_fp:
                    self.rollback()
                    raise CheckpointMismatch(scope)
                owner_ok = row["lease_owner"] in (None, "", owner)
                expired = row["lease_until"] is None or float(row["lease_until"]) <= now
                if owner_ok or expired:
                    self.conn.execute(
                        """
                        UPDATE checkpoints
                        SET lease_owner = ?, lease_until = ?
                        WHERE scope = ?
                        """,
                        (owner, until, scope),
                    )
                    self.commit()
                    return True
                self.commit()
                return False
            except CheckpointMismatch:
                raise
            except Exception:
                self.rollback()
                raise

    def renew_lease(self, scope: str, owner: str, now: float, ttl: float) -> bool:
        """Extend the lease only while this owner still holds it and it has not expired."""
        with self.lock:
            cursor = self.conn.execute(
                """
                UPDATE checkpoints SET lease_until = ?
                WHERE scope = ? AND lease_owner = ? AND lease_until > ?
                """,
                (now + ttl, scope, owner, now),
            )
        return cursor.rowcount == 1

    def _require_lease(self, cursor: sqlite3.Cursor, scope: str) -> None:
        if cursor.rowcount != 1:
            raise LeaseDenied(scope)

    def release_lease(self, scope: str, owner: str) -> None:
        with self.lock:
            self.conn.execute(
                """
                UPDATE checkpoints
                SET lease_owner = NULL, lease_until = NULL
                WHERE scope = ? AND lease_owner = ?
                """,
                (scope, owner),
            )

    def checkpoint(self, scope: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM checkpoints WHERE scope = ?",
                (scope,),
            ).fetchone()
        return _row_dict(row)

    def mark_apply_ahead(
        self, scope: str, owner: str, fetched_token: str, snapshot_id: str
    ) -> None:
        cursor = self.conn.execute(
            """
            UPDATE checkpoints
            SET applied_token = ?, apply_ahead = 1, snapshot_id = ?
            WHERE scope = ? AND lease_owner = ?
            """,
            (fetched_token, snapshot_id, scope, owner),
        )
        self._require_lease(cursor, scope)

    def write_barrier(
        self,
        scope: str,
        owner: str,
        fetched_token: str,
        next_token: str,
        snapshot_id: str,
    ) -> None:
        if self.fail_next_checkpoint:
            self.fail_next_checkpoint = False
            raise CheckpointIOError(scope)
        with self.lock:
            self.begin()
            try:
                done = 1 if next_token == "" else 0
                cursor = self.conn.execute(
                    """
                    UPDATE checkpoints
                    SET consumed_token = ?, next_token = ?, snapshot_id = ?,
                        apply_ahead = 0, done = ?
                    WHERE scope = ? AND lease_owner = ?
                    """,
                    (fetched_token, next_token, snapshot_id, done, scope, owner),
                )
                self._require_lease(cursor, scope)
                self.commit()
            except Exception:
                self.rollback()
                raise

    def apply_rows(
        self,
        caller: str,
        parent: str,
        snapshot_id: str,
        rows: list[dict[str, Any]],
        now: float,
    ) -> None:
        """Idempotent upserts inside the caller's open transaction."""
        for row in rows:
            fields = {
                "beds": row["beds"],
                "holder_label": row["holder_label"],
                "note": row["note"],
                "plot_code": row["plot_code"],
                "resource_id": row["resource_id"],
                "sort_key": row["sort_key"],
                "source_version": row["source_version"],
            }
            key = page_replay_key(
                caller, snapshot_id, fields["resource_id"], fields["source_version"]
            )
            fp = fingerprint(parent, fields)
            prepared = self.prepare_idempotency(caller, key, fp, now)
            if prepared["kind"] == "claim":
                finished = self.finish_idempotency(
                    caller,
                    key,
                    prepared["claim_token"],
                    parent,
                    fields,
                    None,
                    snapshot_id,
                    now,
                )
                if finished["kind"] != "created":
                    raise RuntimeError("export upsert without If-Match did not commit")
            elif prepared["kind"] == "stored":
                continue
            else:
                raise IdempotencyConflict(prepared["kind"])

    def apply_split(
        self,
        scope: str,
        owner: str,
        caller: str,
        parent: str,
        snapshot_id: str,
        rows: list[dict[str, Any]],
        fetched_token: str,
        now: float,
    ) -> None:
        with self.lock:
            self.begin()
            try:
                self.apply_rows(caller, parent, snapshot_id, rows, now)
                self.mark_apply_ahead(scope, owner, fetched_token, snapshot_id)
                self.commit()
            except Exception:
                self.rollback()
                raise

    def apply_atomic(
        self,
        scope: str,
        owner: str,
        caller: str,
        parent: str,
        snapshot_id: str,
        rows: list[dict[str, Any]],
        fetched_token: str,
        next_token: str,
        now: float,
    ) -> None:
        with self.lock:
            self.begin()
            try:
                self.apply_rows(caller, parent, snapshot_id, rows, now)
                if self.fail_next_checkpoint:
                    # Same transaction: the page upserts above roll back with it.
                    self.fail_next_checkpoint = False
                    raise CheckpointIOError(scope)
                done = 1 if next_token == "" else 0
                cursor = self.conn.execute(
                    """
                    UPDATE checkpoints
                    SET consumed_token = ?, next_token = ?, snapshot_id = ?,
                        applied_token = ?, apply_ahead = 0, done = ?
                    WHERE scope = ? AND lease_owner = ?
                    """,
                    (
                        fetched_token,
                        next_token,
                        snapshot_id,
                        fetched_token,
                        done,
                        scope,
                        owner,
                    ),
                )
                self._require_lease(cursor, scope)
                self.commit()
            except Exception:
                self.rollback()
                raise
