"""SQLite frontier. The durable mode commits the in-flight mark before send.

Opening a database does not recover interrupted work. The crawler resets
in-flight rows to pending when a run starts, and it does not refill the
page budget unless the stored horizon has ended.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from incremental_crawl_lab.config import Config
from incremental_crawl_lab.estimate import estimate_rate
from incremental_crawl_lab.observe import Decision

_URL_FIELDS = {
    "origin",
    "depth",
    "importance",
    "status",
    "etag",
    "weak",
    "last_modified",
    "sha256",
    "simhash",
    "charset",
    "charset_unknown",
    "media_type",
    "body",
    "synced_at",
    "first_observed_at",
    "last_observed_at",
    "material_change_count",
    "byte_change_count",
    "cosmetic_change_count",
    "observation_count",
    "lambda_hat",
    "rate_censored",
    "byte_identity_known",
    "consecutive_failures",
    "next_due_at",
    "hold_until",
    "soft_error",
    "variant_accept",
    "variant_lang",
    "not_before",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    url TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    depth INTEGER NOT NULL,
    importance REAL NOT NULL,
    status TEXT NOT NULL,
    etag TEXT,
    weak INTEGER NOT NULL DEFAULT 0,
    last_modified TEXT,
    sha256 TEXT,
    simhash TEXT,
    charset TEXT,
    charset_unknown INTEGER NOT NULL DEFAULT 0,
    media_type TEXT,
    body BLOB,
    synced_at REAL,
    first_observed_at REAL,
    last_observed_at REAL,
    material_change_count INTEGER NOT NULL DEFAULT 0,
    byte_change_count INTEGER NOT NULL DEFAULT 0,
    cosmetic_change_count INTEGER NOT NULL DEFAULT 0,
    observation_count INTEGER NOT NULL DEFAULT 0,
    lambda_hat REAL NOT NULL DEFAULT 0,
    rate_censored INTEGER NOT NULL DEFAULT 0,
    byte_identity_known INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_due_at REAL,
    hold_until REAL,
    soft_error INTEGER NOT NULL DEFAULT 0,
    variant_accept TEXT,
    variant_lang TEXT,
    not_before REAL
);
CREATE TABLE IF NOT EXISTS hosts (
    origin TEXT PRIMARY KEY,
    next_request_at REAL NOT NULL DEFAULT 0,
    retry_attempt INTEGER NOT NULL DEFAULT 0,
    robots_body TEXT,
    robots_fetch_time REAL,
    robots_expiry REAL,
    policy TEXT NOT NULL DEFAULT 'unknown',
    deferred_until REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    page_budget_left INTEGER NOT NULL,
    retry_budget_left INTEGER NOT NULL,
    page_budget INTEGER NOT NULL,
    retry_budget INTEGER NOT NULL,
    horizon_start REAL,
    horizon_end REAL,
    objective TEXT NOT NULL,
    collection_capacity INTEGER NOT NULL,
    min_host_interval REAL NOT NULL
);
"""


def importance_for(depth: int) -> float:
    return 1.0 / (1.0 + int(depth))


class Store:
    def __init__(self, conn: sqlite3.Connection, *, durable: bool) -> None:
        self.conn = conn
        self.durable = durable

    @classmethod
    def open(cls, path: str | None, *, durable: bool) -> "Store":
        if durable:
            if not path:
                raise ValueError("a durable frontier needs a filesystem path")
            conn = sqlite3.connect(path)
            conn.isolation_level = None
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        else:
            conn = sqlite3.connect(":memory:")
            conn.isolation_level = None
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript(_SCHEMA)
        return cls(conn, durable=durable)

    def close(self) -> None:
        if self.conn is None:
            return
        if self.durable:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()
        self.conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def immediate(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _event(self, url: str | None, ts: float, kind: str, detail: dict) -> None:
        self.conn.execute(
            "INSERT INTO events(url, ts, kind, detail) VALUES (?, ?, ?, ?)",
            (url, ts, kind, json.dumps(detail, ensure_ascii=True, sort_keys=True, default=str)),
        )

    def get(self, url: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM urls WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    def host(self, origin: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM hosts WHERE origin=?", (origin,)).fetchone()
        return dict(row) if row else None

    def run_state(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM run_state WHERE id=1").fetchone()
        return dict(row) if row else None

    def events(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def count_urls(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0])

    def occupants(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM urls WHERE status IN ('live', 'pending', 'in_flight')"
        ).fetchone()
        return int(row[0])

    def recover_inflight(self) -> int:
        with self.immediate():
            cursor = self.conn.execute(
                "UPDATE urls SET status='pending' WHERE status='in_flight'"
            )
            return int(cursor.rowcount)

    def begin_horizon(self, now: float, config: Config) -> dict:
        """Stamp the window. Refill budgets only when the previous window has ended."""
        with self.immediate():
            state = self.run_state()
            horizon = float(config.horizon_seconds)
            if state is None:
                self.conn.execute(
                    """INSERT INTO run_state (
                        id, page_budget_left, retry_budget_left, page_budget, retry_budget,
                        horizon_start, horizon_end, objective, collection_capacity, min_host_interval
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        config.page_budget,
                        config.retry_budget,
                        config.page_budget,
                        config.retry_budget,
                        now,
                        now + horizon,
                        config.objective,
                        config.collection_capacity,
                        config.min_host_interval_seconds,
                    ),
                )
            elif state["horizon_end"] is None:
                self.conn.execute(
                    "UPDATE run_state SET horizon_start=?, horizon_end=? WHERE id=1",
                    (now, now + horizon),
                )
            elif now >= float(state["horizon_end"]):
                self.conn.execute(
                    """UPDATE run_state SET
                        page_budget_left=page_budget,
                        retry_budget_left=retry_budget,
                        horizon_start=?,
                        horizon_end=?
                    WHERE id=1""",
                    (now, now + horizon),
                )
                self.conn.execute(
                    "UPDATE hosts SET deferred_until=NULL, retry_attempt=0"
                )
                self.conn.execute("UPDATE urls SET not_before=NULL")
                self.conn.execute(
                    "UPDATE urls SET hold_until=NULL WHERE hold_until IS NOT NULL AND hold_until<=?",
                    (now,),
                )
        state = self.run_state()
        assert state is not None
        return state

    def _ensure_host(self, origin: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO hosts(origin, next_request_at) VALUES (?, 0)",
            (origin,),
        )

    def arm_request(self, origin: str, url: str | None, now: float, gap: float) -> None:
        """Commit the cooldown, and the in-flight mark, before the request is sent."""
        with self.immediate():
            self._ensure_host(origin)
            self.conn.execute(
                "UPDATE hosts SET next_request_at=? WHERE origin=?",
                (now + gap, origin),
            )
            if url is not None:
                updated = self.conn.execute(
                    "UPDATE urls SET status='in_flight' WHERE url=?",
                    (url,),
                )
                if updated.rowcount != 1:
                    raise KeyError(url)

    def host_next(self, origin: str) -> float:
        row = self.host(origin)
        if row is None:
            return 0.0
        return float(row["next_request_at"] or 0.0)

    def host_deferred(self, origin: str, now: float) -> bool:
        row = self.host(origin)
        if row is None or row["deferred_until"] is None:
            return False
        return float(row["deferred_until"]) > now

    def robots_fresh(self, origin: str, now: float) -> bool:
        row = self.host(origin)
        if row is None or row["robots_expiry"] is None:
            return False
        return float(row["robots_expiry"]) > now

    def save_robots(
        self,
        origin: str,
        *,
        body: str,
        fetched_at: float,
        expiry: float,
        policy: str,
        now: float,
    ) -> None:
        with self.immediate():
            self._ensure_host(origin)
            self.conn.execute(
                """UPDATE hosts SET robots_body=?, robots_fetch_time=?, robots_expiry=?, policy=?
                   WHERE origin=?""",
                (body, fetched_at, expiry, policy, origin),
            )
            self._event(origin + "/robots.txt", now, "robots_policy", {"policy": policy})

    def note_robots_fetch(self, url: str, now: float, status: int) -> None:
        with self.immediate():
            self._event(url, now, "robots_fetch", {"status": status})

    def mark_disallowed(self, url: str, now: float) -> bool:
        with self.immediate():
            row = self.get(url)
            if row is None or row["status"] == "disallowed":
                return False
            self.conn.execute(
                "UPDATE urls SET status='disallowed', not_before=NULL WHERE url=?",
                (url,),
            )
            self._event(url, now, "robots_block", {})
            return True

    def list_new(self, now: float) -> list[dict]:
        return self._select_urls(
            """SELECT * FROM urls
               WHERE status='pending' AND observation_count=0 AND consecutive_failures=0
                 AND (hold_until IS NULL OR hold_until<=?)
               ORDER BY depth, url""",
            (now,),
        )

    def list_due(self, now: float) -> list[dict]:
        return self._select_urls(
            """SELECT * FROM urls
               WHERE status='live' AND next_due_at IS NOT NULL AND next_due_at<=?
                 AND (hold_until IS NULL OR hold_until<=?)
               ORDER BY synced_at, url""",
            (now, now),
        )

    def list_retries(self, now: float) -> list[dict]:
        return self._select_urls(
            """SELECT * FROM urls
               WHERE status='pending' AND consecutive_failures>0
                 AND (hold_until IS NULL OR hold_until<=?)
               ORDER BY depth, url""",
            (now,),
        )

    def list_status(self, status: str) -> list[dict]:
        return self._select_urls(
            "SELECT * FROM urls WHERE status=? ORDER BY url",
            (status,),
        )

    def _select_urls(self, sql: str, args: tuple) -> list[dict]:
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def apply_decision(
        self,
        url: str,
        decision: Decision,
        now: float,
        *,
        sample_interval: float,
    ) -> bool:
        """Apply a terminal result. A second call is a no-op and does not charge again."""
        with self.immediate():
            row = self.get(url)
            if row is None or row["status"] != "in_flight":
                return False
            state = self.run_state()
            if state is None:
                raise RuntimeError("run_state is missing")
            if decision.charges_page and int(state["page_budget_left"]) <= 0:
                raise RuntimeError("page budget exhausted")
            material = int(row["material_change_count"]) + decision.material_delta
            byte_count = int(row["byte_change_count"]) + decision.byte_delta
            cosmetic = int(row["cosmetic_change_count"]) + decision.cosmetic_delta
            observed = int(row["observation_count"])
            first = row["first_observed_at"]
            last = row["last_observed_at"]
            if decision.counts_observation:
                observed += 1
                if first is None:
                    first = now
                last = now
            assignments = {
                "status": decision.status,
                "material_change_count": material,
                "byte_change_count": byte_count,
                "cosmetic_change_count": cosmetic,
                "observation_count": observed,
                "first_observed_at": first,
                "last_observed_at": last,
                "soft_error": 1 if decision.soft_error else int(row["soft_error"]),
            }
            if decision.updates_sync:
                # Due at the next decision boundary. A sync a few seconds after
                # the boundary must still be eligible when that boundary arrives.
                assignments["synced_at"] = now
                assignments["next_due_at"] = float(state["horizon_end"])
            if decision.reset_failures:
                assignments["consecutive_failures"] = 0
                assignments["not_before"] = None
                assignments["hold_until"] = None
            if decision.byte_identity_known is not None:
                assignments["byte_identity_known"] = 1 if decision.byte_identity_known else 0
            if decision.status == "gone":
                assignments["body"] = None
            for key, value in decision.columns.items():
                if key not in _URL_FIELDS:
                    raise KeyError(key)
                if isinstance(value, bool):
                    value = 1 if value else 0
                assignments[key] = value
            if decision.counts_observation:
                span = 0.0 if first is None or last is None else float(last) - float(first)
                estimate = estimate_rate(
                    material,
                    span,
                    max(0, observed - 1),
                    sample_interval,
                )
                assignments["lambda_hat"] = estimate.lam
                assignments["rate_censored"] = 1 if estimate.censored else 0
            keys = list(assignments)
            sql = "UPDATE urls SET " + ", ".join(f"{key}=?" for key in keys) + " WHERE url=?"
            self.conn.execute(sql, [assignments[key] for key in keys] + [url])
            if decision.charges_page:
                self.conn.execute(
                    "UPDATE run_state SET page_budget_left=page_budget_left-1 WHERE id=1"
                )
            # A finished observation resets the host's backoff attempt.
            self.conn.execute(
                "UPDATE hosts SET retry_attempt=0 WHERE origin=?",
                (row["origin"],),
            )
            detail = {
                "oracle_sync": decision.oracle_sync,
                "material_delta": decision.material_delta,
                "byte_delta": decision.byte_delta,
                "cosmetic_delta": decision.cosmetic_delta,
                "distance": decision.distance,
                "charset_unknown": decision.charset_unknown,
                "byte_identity_known": decision.byte_identity_known,
            }
            self._event(url, now, decision.kind, detail)
            if decision.charset_unknown and decision.kind != "charset_unknown":
                self._event(url, now, "charset_unknown", {})
            return True

    def fail_retry(
        self,
        url: str,
        origin: str,
        now: float,
        *,
        delay: float,
        defer_after: float,
        horizon_end: float,
        reason: str,
    ) -> str:
        """Return ``retry`` or ``deferred``. Page budget is left unchanged."""
        with self.immediate():
            row = self.get(url)
            if row is None or row["status"] != "in_flight":
                return "ignored"
            state = self.run_state()
            if state is None:
                raise RuntimeError("run_state is missing")
            self._ensure_host(origin)
            host = self.host(origin)
            assert host is not None
            attempt = int(host["retry_attempt"]) + 1
            failures = int(row["consecutive_failures"]) + 1
            left = int(state["retry_budget_left"])
            if left <= 0 or delay > defer_after:
                self.conn.execute(
                    """UPDATE urls SET status=?, consecutive_failures=?, hold_until=?
                       WHERE url=?""",
                    (
                        "pending" if int(row["observation_count"]) == 0 else "live",
                        failures,
                        horizon_end,
                        url,
                    ),
                )
                self.conn.execute(
                    "UPDATE hosts SET retry_attempt=?, deferred_until=? WHERE origin=?",
                    (attempt, horizon_end, origin),
                )
                self._event(url, now, "deferred", {"reason": reason, "delay": delay})
                return "deferred"
            current_next = float(host["next_request_at"] or 0.0)
            nxt = max(current_next, now + delay)
            restored = "pending" if int(row["observation_count"]) == 0 else "live"
            self.conn.execute(
                """UPDATE urls SET status=?, consecutive_failures=?, not_before=?
                   WHERE url=?""",
                (restored, failures, nxt, url),
            )
            self.conn.execute(
                "UPDATE hosts SET retry_attempt=?, next_request_at=? WHERE origin=?",
                (attempt, nxt, origin),
            )
            self.conn.execute(
                "UPDATE run_state SET retry_budget_left=retry_budget_left-1 WHERE id=1"
            )
            self._event(url, now, "retry", {"reason": reason, "delay": delay, "attempt": attempt})
            return "retry"

    def park(self, url: str, until: float, now: float, kind: str, detail: dict) -> None:
        """Hold a URL that has not been armed, so a closed retry budget cannot spin."""
        with self.immediate():
            row = self.get(url)
            if row is None:
                return
            status = row["status"]
            if status == "in_flight":
                status = "pending" if int(row["observation_count"]) == 0 else "live"
            elif status not in ("pending", "live"):
                return
            self.conn.execute(
                "UPDATE urls SET status=?, hold_until=? WHERE url=?",
                (status, until, url),
            )
            self._event(url, now, kind, detail)

    def hold(self, url: str, until: float, now: float, kind: str, detail: dict) -> bool:
        with self.immediate():
            row = self.get(url)
            if row is None or row["status"] != "in_flight":
                return False
            self.conn.execute(
                "UPDATE urls SET status='pending', hold_until=? WHERE url=?",
                (until, url),
            )
            self._event(url, now, kind, detail)
            return True

    def admit(
        self,
        url: str,
        *,
        origin: str,
        depth: int,
        now: float,
        capacity: int,
        protected: set[str],
    ) -> str:
        """Return admitted, exists, disallowed, or rejected. Rejected URLs are not stored."""
        weight = importance_for(depth)
        with self.immediate():
            current = self.get(url)
            if current is not None and current["status"] in ("live", "pending", "in_flight"):
                return "exists"
            if current is not None and current["status"] == "disallowed":
                return "disallowed"
            occupants = self._select_urls(
                "SELECT * FROM urls WHERE status IN ('live', 'pending', 'in_flight')",
                (),
            )
            if len(occupants) >= capacity:
                victims = [
                    row
                    for row in occupants
                    if row["status"] != "in_flight"
                    and row["url"] not in protected
                    and row["url"] != url
                ]
                victims.sort(key=lambda row: (row["importance"], -row["depth"], row["url"]))
                if not victims or weight <= float(victims[0]["importance"]):
                    return "rejected"
                victim = victims[0]
                self.conn.execute(
                    "UPDATE urls SET status='evicted' WHERE url=?",
                    (victim["url"],),
                )
                self._event(victim["url"], now, "evicted", {"replaced_by": url})
            if current is None:
                self.conn.execute(
                    """INSERT INTO urls(url, origin, depth, importance, status)
                       VALUES (?, ?, ?, ?, 'pending')""",
                    (url, origin, depth, weight),
                )
            else:
                self.conn.execute(
                    """UPDATE urls SET origin=?, depth=?, importance=?, status='pending',
                        etag=NULL, weak=0, last_modified=NULL, sha256=NULL, simhash=NULL,
                        charset=NULL, charset_unknown=0, media_type=NULL, body=NULL,
                        synced_at=NULL, first_observed_at=NULL, last_observed_at=NULL,
                        material_change_count=0, byte_change_count=0, cosmetic_change_count=0,
                        observation_count=0, lambda_hat=0, rate_censored=0,
                        byte_identity_known=0, consecutive_failures=0, next_due_at=NULL,
                        hold_until=NULL, soft_error=0, variant_accept=NULL, variant_lang=NULL,
                        not_before=NULL
                       WHERE url=?""",
                    (origin, depth, weight, url),
                )
            self._event(url, now, "admitted", {"depth": depth, "importance": weight})
            return "admitted"

    def seed_live(self, url: str, **fields) -> None:
        origin = fields.pop("origin")
        depth = int(fields.pop("depth"))
        weight = float(fields.pop("importance", importance_for(depth)))
        columns = {
            "origin": origin,
            "depth": depth,
            "importance": weight,
            "status": fields.pop("status", "live"),
        }
        columns.update(fields)
        unknown = set(columns) - _URL_FIELDS
        if unknown:
            raise KeyError(sorted(unknown))
        names = ["url", *columns.keys()]
        sql = (
            "INSERT INTO urls("
            + ", ".join(names)
            + ") VALUES ("
            + ", ".join("?" for _ in names)
            + ")"
        )
        with self.immediate():
            self.conn.execute(sql, [url, *columns.values()])

    def record(self, url: str | None, now: float, kind: str, detail: dict) -> None:
        with self.immediate():
            self._event(url, now, kind, detail)
