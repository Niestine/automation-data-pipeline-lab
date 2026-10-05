"""Single-store inbox, memo, and outbox.

The claim is the atomic insert. A second delivery sees the row and does
not run the effect. The short freshness map is allowed to forget an id;
the inbox is not. Memo keys follow the shape ``(0, (event_id, step))``:
a hit returns the stored value and writes nothing.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .horizons import INBOX_RETENTION_SECONDS, TOLERANCE_SECONDS
from .schema import Notice

LOG_FIELDS = ("time", "source", "method", "status", "event_id", "event_type", "latency_ms")


@dataclass
class InboxRow:
    event_id: str
    status: str
    notice: Notice
    accepted_at: int
    route_id: str

    def to_json(self) -> dict:
        return {
            "event_id": self.event_id,
            "status": self.status,
            "notice": self.notice.to_json(),
            "accepted_at": self.accepted_at,
            "route_id": self.route_id,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "InboxRow":
        return cls(
            event_id=payload["event_id"],
            status=payload["status"],
            notice=Notice.from_json(payload["notice"]),
            accepted_at=payload["accepted_at"],
            route_id=payload["route_id"],
        )


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inbox: dict[str, InboxRow] = {}
        self.memos: dict[tuple, dict] = {}
        self.releases: dict[str, dict] = {}
        self.outbox: dict[str, dict] = {}
        self.fresh: dict[str, int] = {}
        self.pair_seen: dict[tuple[str, str], str] = {}
        self.last_created: dict[str, int] = {}
        self.logs: list[dict] = []
        self.release_changes = 0
        self.memo_hits = 0
        self.memo_misses = 0
        self.claim_inserts = 0
        self.claim_duplicates = 0
        self.fetches = 0

    def claim(self, row: InboxRow) -> str:
        with self.lock:
            current = self.inbox.get(row.event_id)
            if current is not None and current.status in ("queued", "ready", "done"):
                self.claim_duplicates += 1
                return "duplicate"
            self.inbox[row.event_id] = row
            self.claim_inserts += 1
            return "inserted"

    def fresh_seen(self, event_id: str, now: int) -> bool:
        with self.lock:
            expires = self.fresh.get(event_id)
            if expires is None:
                return False
            if now > expires:
                self.fresh.pop(event_id, None)
                return False
            return True

    def fresh_remember(self, event_id: str, timestamp: int, tolerance: int = TOLERANCE_SECONDS) -> None:
        with self.lock:
            self.fresh[event_id] = int(timestamp) + int(tolerance)

    def forget_fresh(self) -> None:
        with self.lock:
            self.fresh.clear()

    def add_log(self, **fields: object) -> None:
        with self.lock:
            self.logs.append({key: fields.get(key) for key in LOG_FIELDS})

    def retained(self, accepted_at: int, now: int) -> bool:
        return now - accepted_at <= INBOX_RETENTION_SECONDS

    def prune(self, now: int) -> list[str]:
        """Drop finished rows past the 30-day horizon, with their memo and outbox.

        Rows that are still queued or ready stay, whatever their age.
        """

        with self.lock:
            expired = [
                event_id
                for event_id, row in self.inbox.items()
                if row.status == "done" and not self.retained(row.accepted_at, now)
            ]
            for event_id in expired:
                del self.inbox[event_id]
                self.memos.pop((0, (event_id, "apply")), None)
                self.outbox.pop(event_id, None)
            return expired

    def snapshot(self) -> dict:
        with self.lock:
            memos = []
            for key, value in self.memos.items():
                _zero, pair = key
                event_id, step = pair
                memos.append({"event_id": event_id, "step": step, "value": value})
            return {
                "version": 1,
                "inbox": [row.to_json() for row in self.inbox.values()],
                "memos": memos,
                "releases": self.releases,
                "outbox": self.outbox,
                "pair_seen": [
                    {"object_id": object_id, "event_type": event_type, "event_id": event_id}
                    for (object_id, event_type), event_id in self.pair_seen.items()
                ],
                "last_created": self.last_created,
                "release_changes": self.release_changes,
                "memo_hits": self.memo_hits,
                "memo_misses": self.memo_misses,
            }

    def restore(self, payload: dict) -> None:
        if payload.get("version") != 1:
            raise ValueError("unsupported snapshot version")
        with self.lock:
            self.inbox = {row["event_id"]: InboxRow.from_json(row) for row in payload["inbox"]}
            self.memos = {}
            for item in payload["memos"]:
                self.memos[(0, (item["event_id"], item["step"]))] = item["value"]
            self.releases = {key: dict(value) for key, value in payload["releases"].items()}
            self.outbox = {key: dict(value) for key, value in payload["outbox"].items()}
            self.pair_seen = {
                (item["object_id"], item["event_type"]): item["event_id"] for item in payload["pair_seen"]
            }
            self.last_created = {key: int(value) for key, value in payload["last_created"].items()}
            self.release_changes = int(payload["release_changes"])
            self.memo_hits = int(payload["memo_hits"])
            self.memo_misses = int(payload["memo_misses"])
            self.fresh = {}

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(self.snapshot(), separators=(",", ":"), sort_keys=True).encode("utf-8")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, path)

    def load(self, path: Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.restore(payload)
