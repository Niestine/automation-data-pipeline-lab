"""One-step element states for the email index and the status column.

The state order is absent, delete-only, write-only, public. A published
version moves each element by at most one step. ``delete_only`` removes a
key on delete and does not add one. ``write_only`` maintains the key and is
not read. ``public`` is the only state readers may use. Status becomes
required only after a durable backfill commit; the email index disappears
only after a durable cleanup commit.

The matrix is the lab's contract for the orphan-index failure shown when
two versions are live at once. It is not a quotation of the F1 paper.
"""

from __future__ import annotations

from dataclasses import dataclass

from recovery_lab.errors import MigrationError, SimulatedCrash

STATE_ORDER = ("absent", "delete_only", "write_only", "public")

VERSIONS: dict[int, dict[str, object]] = {
    1: {"email_index": "public", "status": "absent"},
    2: {"email_index": "public", "status": "delete_only"},
    3: {"email_index": "public", "status": "write_only"},
    4: {"email_index": "public", "status": "write_only"},
    5: {"email_index": "public", "status": "public"},
    6: {"email_index": "write_only", "status": "public"},
    7: {"email_index": "delete_only", "status": "public"},
    8: {"email_index": "delete_only", "status": "public"},
    9: {"email_index": "absent", "status": "public"},
}


def states_for(version: int) -> dict[str, str]:
    spec = VERSIONS[version]
    return {"email_index": str(spec["email_index"]), "status": str(spec["status"])}


def _distance(left: str, right: str) -> int:
    return abs(STATE_ORDER.index(left) - STATE_ORDER.index(right))


def assert_transition(ledger, target: int) -> None:
    """Reject a skip, a required column before backfill, or a drop that skips write-only."""
    current = int(ledger.schema_version)
    if target not in VERSIONS or target != current + 1:
        raise MigrationError(f"schema version {current} cannot move to {target}")
    current_spec = VERSIONS[current]
    target_spec = VERSIONS[target]
    if _distance(str(current_spec["email_index"]), str(target_spec["email_index"])) > 1:
        raise MigrationError("email_index would skip a state")
    if _distance(str(current_spec["status"]), str(target_spec["status"])) > 1:
        raise MigrationError("status would skip a state")
    if target_spec["email_index"] == "delete_only" and current_spec["email_index"] == "public":
        raise MigrationError("email_index cannot become delete_only while the neighbor is public")
    if target == 4:
        missing = [row for row in ledger.accounts.values() if not row.get("status")]
        if missing:
            raise MigrationError("backfill is incomplete")
    if target_spec["status"] == "public" and not ledger.backfill_complete:
        raise MigrationError("status cannot become public before backfill_complete is durable")
    if target == 8 and ledger.email_index:
        raise MigrationError("cleanup is incomplete")
    if target_spec["email_index"] == "absent" and not ledger.cleanup_complete:
        raise MigrationError("email_index cannot become absent before cleanup_complete is durable")


@dataclass
class Integrity:
    orphan_index_keys: int
    missing_public_index: int
    rows_missing_status: int

    @property
    def anomaly_count(self) -> int:
        return self.orphan_index_keys + self.missing_public_index + self.rows_missing_status


def integrity(ledger, *, email_public: bool, status_public: bool) -> Integrity:
    orphans = 0
    for email, account_id in ledger.email_index.items():
        account = ledger.accounts.get(account_id)
        if account is None or account.get("email") != email:
            orphans += 1
    missing = 0
    if email_public:
        for account in ledger.accounts.values():
            if ledger.email_index.get(account["email"]) != account["account_id"]:
                missing += 1
    missing_status = 0
    if status_public:
        for account in ledger.accounts.values():
            if not account.get("status"):
                missing_status += 1
    return Integrity(orphans, missing, missing_status)


class Migrator:
    """Drive the version table. Each call moves one version."""

    def __init__(self, store) -> None:
        self.store = store

    def step(self) -> None:
        self.store._load_idle()
        target = int(self.store.ledger.schema_version) + 1
        if target == 4:
            self.backfill()
            self.store.transition(4, marker="backfill_complete")
            return
        if target == 8:
            self.cleanup()
            self.store.transition(8, marker="cleanup_complete")
            return
        self.store.transition(target)

    def advance_to(self, target: int) -> None:
        while int(self.store.ledger.schema_version) < target:
            self.step()
            self.store._load_idle()

    def backfill(self, crash_after: int | None = None) -> int:
        """Fill ``status`` one committed row at a time. Resume skips filled rows."""
        filled = 0
        while True:
            self.store._load_idle()
            pending = sorted(
                account_id
                for account_id, account in self.store.ledger.accounts.items()
                if not account.get("status")
            )
            if not pending:
                return filled
            account_id = pending[0]
            account = self.store.ledger.accounts[account_id]
            self.store.upsert(account_id, account["balance"], account["email"], status="active")
            self.store.commit()
            filled += 1
            if crash_after is not None and filled > crash_after:
                raise SimulatedCrash(f"backfill stopped after {filled} row commits")

    def cleanup(self) -> int:
        removed = 0
        while True:
            self.store._load_idle()
            if not self.store.ledger.email_index:
                return removed
            email = sorted(self.store.ledger.email_index)[0]
            self.store.clear_email_key(email)
            self.store.commit()
            removed += 1
