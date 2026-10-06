"""SQLite state for grants, orders, checkpoints, and webhook ids."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from lotcycle.clock import VirtualClock

_FORBIDDEN_LOG_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "client_secret",
        "refresh_token",
        "secret",
        "sender_proof",
    }
)


class Store:
    def __init__(self, path: str, clock: VirtualClock) -> None:
        self.clock = clock
        self.path = path
        self._lock = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.logs: list[dict] = []
        self._schema()

    def close(self) -> None:
        self.con.close()

    def log(self, event: str, **fields: object) -> None:
        overlap = _FORBIDDEN_LOG_KEYS.intersection(fields)
        if overlap:
            raise ValueError("log field " + ",".join(sorted(overlap)))
        self.logs.append({"at": self.clock.now(), "event": event, **fields})

    @contextmanager
    def transaction(self):
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.con.rollback()
                raise
            else:
                self.con.commit()

    def _schema(self) -> None:
        self.con.executescript(
            """
            CREATE TABLE clients (
                client_id TEXT PRIMARY KEY,
                client_type TEXT NOT NULL,
                secret TEXT,
                scope TEXT NOT NULL,
                audience TEXT NOT NULL,
                mode TEXT NOT NULL,
                refresh_allowed INTEGER NOT NULL,
                sender_key_id TEXT
            );
            CREATE TABLE families (
                family_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                status TEXT NOT NULL,
                active_generation INTEGER,
                last_used_at REAL NOT NULL,
                scope TEXT NOT NULL,
                audience TEXT NOT NULL,
                mode TEXT NOT NULL
            );
            CREATE TABLE refresh_tokens (
                token_hash TEXT PRIMARY KEY,
                family_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                consumed_at REAL
            );
            CREATE TABLE access_tokens (
                token_hash TEXT PRIMARY KEY,
                family_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                issuer TEXT NOT NULL,
                audience TEXT NOT NULL,
                scope TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE reuse_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                presenter TEXT NOT NULL,
                at REAL NOT NULL
            );
            CREATE TABLE idempotency (
                client_id TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                idem_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                response_status INTEGER,
                response_type TEXT,
                response_body BLOB,
                created_at REAL NOT NULL,
                PRIMARY KEY (client_id, method, path, idem_key)
            );
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                qty INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE local_entries (
                client_id TEXT NOT NULL,
                audience TEXT NOT NULL,
                entry_id TEXT NOT NULL,
                updated REAL NOT NULL,
                doc_updated REAL NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (client_id, audience, entry_id)
            );
            CREATE TABLE checkpoints (
                client_id TEXT NOT NULL,
                audience TEXT NOT NULL,
                cursor TEXT NOT NULL,
                synced_at REAL NOT NULL,
                PRIMARY KEY (client_id, audience, cursor)
            );
            CREATE TABLE webhook_seen (
                endpoint_id TEXT NOT NULL,
                webhook_id TEXT NOT NULL,
                received_at REAL NOT NULL,
                resource_id TEXT,
                PRIMARY KEY (endpoint_id, webhook_id)
            );
            """
        )

    def insert_client(self, row: dict) -> None:
        with self._lock:
            self.con.execute(
                """
                INSERT INTO clients (
                    client_id, client_type, secret, scope, audience, mode,
                    refresh_allowed, sender_key_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["client_id"],
                    row["client_type"],
                    row.get("secret"),
                    row["scope"],
                    row["audience"],
                    row["mode"],
                    1 if row.get("refresh_allowed", True) else 0,
                    row.get("sender_key_id"),
                ),
            )

    def get_client(self, client_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.con.execute(
                "SELECT * FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()

    def insert_family(self, row: dict) -> None:
        with self._lock:
            self.con.execute(
                """
                INSERT INTO families (
                    family_id, client_id, status, active_generation, last_used_at,
                    scope, audience, mode
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (
                    row["family_id"],
                    row["client_id"],
                    row["generation"],
                    row["last_used_at"],
                    row["scope"],
                    row["audience"],
                    row["mode"],
                ),
            )

    def get_family(self, family_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.con.execute(
                "SELECT * FROM families WHERE family_id = ?", (family_id,)
            ).fetchone()

    def family_for_client(self, client_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.con.execute(
                "SELECT * FROM families WHERE client_id = ?", (client_id,)
            ).fetchone()

    def insert_refresh(self, token_hash: str, family_id: str, generation: int) -> None:
        self.con.execute(
            """
            INSERT INTO refresh_tokens (token_hash, family_id, generation, consumed_at)
            VALUES (?, ?, ?, NULL)
            """,
            (token_hash, family_id, generation),
        )

    def find_refresh(self, token_hash: str) -> sqlite3.Row | None:
        with self._lock:
            return self.con.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()

    def active_refresh_count(self, family_id: str) -> int:
        with self._lock:
            row = self.con.execute(
                """
                SELECT COUNT(*) AS n FROM refresh_tokens
                WHERE family_id = ? AND consumed_at IS NULL
                """,
                (family_id,),
            ).fetchone()
        return int(row["n"])

    def insert_access_row(self, row: dict) -> None:
        self.con.execute(
            """
            INSERT INTO access_tokens (
                token_hash, family_id, client_id, issuer, audience, scope, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["token_hash"],
                row["family_id"],
                row["client_id"],
                row["issuer"],
                row["audience"],
                row["scope"],
                row["expires_at"],
            ),
        )

    def reuse_count(self, family_id: str) -> int:
        with self._lock:
            row = self.con.execute(
                "SELECT COUNT(*) AS n FROM reuse_log WHERE family_id = ?",
                (family_id,),
            ).fetchone()
        return int(row["n"])

    def order_count(self, client_id: str | None = None) -> int:
        with self._lock:
            if client_id is None:
                row = self.con.execute("SELECT COUNT(*) AS n FROM orders").fetchone()
            else:
                row = self.con.execute(
                    "SELECT COUNT(*) AS n FROM orders WHERE client_id = ?",
                    (client_id,),
                ).fetchone()
        return int(row["n"])

    def next_order_id(self) -> str:
        row = self.con.execute("SELECT COUNT(*) AS n FROM orders").fetchone()
        return f"ord-{int(row['n']) + 1:04d}"

    def idempotency_row(self, client_id: str, method: str, path: str, key: str):
        return self.con.execute(
            """
            SELECT * FROM idempotency
            WHERE client_id = ? AND method = ? AND path = ? AND idem_key = ?
            """,
            (client_id, method, path, key),
        ).fetchone()

    def read_idempotency(self, client_id: str, method: str, path: str, key: str):
        with self._lock:
            return self.idempotency_row(client_id, method, path, key)

    def idempotency_count(self) -> int:
        with self._lock:
            row = self.con.execute("SELECT COUNT(*) AS n FROM idempotency").fetchone()
        return int(row["n"])

    def checkpoint_count(self, client_id: str, audience: str) -> int:
        with self._lock:
            row = self.con.execute(
                """
                SELECT COUNT(*) AS n FROM checkpoints
                WHERE client_id = ? AND audience = ?
                """,
                (client_id, audience),
            ).fetchone()
        return int(row["n"])

    def checkpointed(self, client_id: str, audience: str, cursor: str) -> bool:
        with self._lock:
            row = self.con.execute(
                """
                SELECT 1 FROM checkpoints
                WHERE client_id = ? AND audience = ? AND cursor = ?
                """,
                (client_id, audience, cursor),
            ).fetchone()
        return row is not None

    def local_ids(self, client_id: str, audience: str) -> set[str]:
        with self._lock:
            rows = self.con.execute(
                """
                SELECT entry_id FROM local_entries
                WHERE client_id = ? AND audience = ?
                """,
                (client_id, audience),
            ).fetchall()
        return {row["entry_id"] for row in rows}

    def local_entry(self, client_id: str, audience: str, entry_id: str):
        with self._lock:
            return self.con.execute(
                """
                SELECT * FROM local_entries
                WHERE client_id = ? AND audience = ? AND entry_id = ?
                """,
                (client_id, audience, entry_id),
            ).fetchone()

    def webhook_count(self, endpoint_id: str) -> int:
        with self._lock:
            row = self.con.execute(
                "SELECT COUNT(*) AS n FROM webhook_seen WHERE endpoint_id = ?",
                (endpoint_id,),
            ).fetchone()
        return int(row["n"])
