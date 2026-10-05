"""Ledger transactions, write-ahead commit, and the bad publish switch."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Callable

from recovery_lab.errors import ConstraintError, IllegalRead, MigrationError, UnavailableSchemaGap
from recovery_lab.faults import Trace
from recovery_lab.format import LOG_NAME, SNAP_NAME, Record, encode_record, parse_log
from recovery_lab.log import discard_torn_tail
from recovery_lab.migrate import VERSIONS, assert_transition, states_for
from recovery_lab.publish import assert_same_volume, publish_file
from recovery_lab.recover import load_ledger
from recovery_lab.snapshot import Ledger, apply_image, encode_snapshot
from recovery_lab.trace import attach, detach, emit

StatFn = Callable[[Path], os.stat_result]
ReplaceFn = Callable[[Path, Path], None]


class LedgerStore:
    """Checksummed account ledger with one append-only log.

    Model Z is the clean power-fault cut (Zheng et al.): completed flushes
    stay, an interrupted write has no effect, and a later operation is kept
    only when it does not depend on a dropped one. Model P is separate
    (Pillai et al.): a garbage sector, a size-before-data extension, and a
    rename that is not ordered behind a flush are scored on their own.

    One checksummed log file does not survive loss of that file. Directory
    fsync is best-effort off POSIX; a failed directory fsync is recorded and
    is not treated as a failed commit. ``publish_before_log_flush`` is a
    test-only switch that publishes the snapshot before the commit record is
    forced, so the crash oracle can show the missing edge. A stored schema
    more than one version away from this process is refused and the files
    are not rewritten.
    """

    def __init__(
        self,
        directory,
        *,
        schema_version: int = 1,
        publish_before_log_flush: bool = False,
        email_state: str | None = None,
        status_state: str | None = None,
        stat_fn: StatFn | None = None,
        replace_fn: ReplaceFn | None = None,
    ) -> None:
        if schema_version not in VERSIONS and email_state is None:
            raise MigrationError(f"unknown schema version {schema_version}")
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.code_version = schema_version
        self.publish_before_log_flush = publish_before_log_flush
        self.email_state = email_state
        self.status_state = status_state
        self.stat_fn = stat_fn
        self.replace_fn = replace_fn
        self.trace = Trace()
        self.trace_from_empty = not (self.root / LOG_NAME).exists() and not (self.root / SNAP_NAME).exists()
        self.last_log_write: int | None = None
        self.log_offset = 0
        self.pending: list[tuple[dict, dict]] = []
        self._tx_open = False
        self._tx_id = 0
        self.next_lsn = 1
        self.next_tx = 1
        self.illegal_reads = 0
        self._handler = attach(self.root)
        self._closed = False
        try:
            self.ledger = load_ledger(self.root, code_version=self.code_version)
            self._refresh_ids()
            self.log_offset = self._disk_log_len()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        detach(self._handler)
        self._handler = None

    def __enter__(self) -> LedgerStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _email(self) -> str:
        if self.email_state:
            return self.email_state
        return VERSIONS[self.code_version]["email_index"]

    def _status(self) -> str:
        if self.status_state:
            return self.status_state
        return VERSIONS[self.code_version]["status"]

    def _disk_log_len(self) -> int:
        path = self.root / LOG_NAME
        return path.stat().st_size if path.exists() else 0

    def _refresh_ids(self) -> None:
        blob = (self.root / LOG_NAME).read_bytes() if (self.root / LOG_NAME).exists() else b""
        records = parse_log(blob)
        if not records:
            self.next_lsn = 1
            self.next_tx = 1
            return
        self.next_lsn = records[-1].lsn + 1
        self.next_tx = max(record.tx_id for record in records) + 1

    def _load_idle(self) -> None:
        if self._tx_open:
            return
        self.ledger = load_ledger(self.root, code_version=self.code_version)
        self._refresh_ids()
        self.log_offset = self._disk_log_len()

    def _begin(self) -> None:
        if self._tx_open:
            return
        self._load_idle()
        self._tx_open = True
        self._tx_id = self.next_tx
        self.next_tx += 1
        self.pending = []

    def read_email(self, email: str) -> int | None:
        if self._email() != "public":
            self.illegal_reads += 1
            raise IllegalRead(f"email_index is {self._email()}")
        self._load_idle()
        return self.ledger.email_index.get(email)

    def read_status(self, account_id: int) -> str | None:
        if self._status() != "public":
            self.illegal_reads += 1
            raise IllegalRead(f"status is {self._status()}")
        self._load_idle()
        account = self.ledger.accounts.get(account_id)
        if account is None:
            return None
        return account.get("status")

    def upsert(
        self,
        account_id: int,
        balance: int,
        email: str,
        status: str | None = None,
    ) -> None:
        self._check_row(account_id, balance, email)
        self._load_idle()
        for other in self.ledger.accounts.values():
            if other["email"] == email and other["account_id"] != account_id:
                raise ConstraintError(f"email is already used by account {other['account_id']}")
        self._begin()
        old = self.ledger.accounts.get(account_id)
        new_account = {
            "account_id": account_id,
            "balance": balance,
            "email": email,
            "status": self._status_value(old, status),
        }
        after_set, after_clear = self._index_after(old, new_account)
        before_set, before_clear = self._index_before(old, after_set)
        before = {
            "account_id": account_id,
            "account": copy.deepcopy(old) if old else None,
            "index_set": before_set,
            "index_clear": before_clear,
        }
        after = {
            "account_id": account_id,
            "account": new_account,
            "index_set": after_set,
            "index_clear": after_clear,
        }
        self.pending.append((before, after))
        apply_image(self.ledger, after)

    def delete(self, account_id: int) -> None:
        self._load_idle()
        if account_id not in self.ledger.accounts:
            raise ConstraintError(f"account {account_id} does not exist")
        self._begin()
        old = self.ledger.accounts[account_id]
        email = old["email"]
        clear: list[str] = []
        if self._email() in ("delete_only", "write_only", "public"):
            if self.ledger.email_index.get(email) == account_id:
                clear.append(email)
        before_set = {}
        if self.ledger.email_index.get(email) == account_id:
            before_set[email] = account_id
        before = {
            "account_id": account_id,
            "account": copy.deepcopy(old),
            "index_set": before_set,
            "index_clear": [],
        }
        after = {
            "account_id": account_id,
            "account": None,
            "index_set": {},
            "index_clear": clear,
        }
        self.pending.append((before, after))
        apply_image(self.ledger, after)

    def clear_email_key(self, email: str) -> None:
        """Remove one index key and keep the account. Used by index cleanup."""
        self._load_idle()
        account_id = self.ledger.email_index.get(email)
        if account_id is None:
            return
        self._begin()
        account = self.ledger.accounts.get(account_id)
        before = {
            "account_id": account_id,
            "account": copy.deepcopy(account) if account else None,
            "index_set": {email: account_id},
            "index_clear": [],
        }
        after = {
            "account_id": account_id,
            "account": copy.deepcopy(account) if account else None,
            "index_set": {},
            "index_clear": [email],
        }
        self.pending.append((before, after))
        apply_image(self.ledger, after)

    def commit(self, marker: str | None = None) -> int | None:
        """Append the open transaction, force the log, and publish the snapshot.

        The default order forces the commit record before ``os.replace``.
        ``publish_before_log_flush`` publishes first. Either way a completed
        call leaves both the log and the snapshot on disk.
        """
        if not self._tx_open or not self.pending:
            self._tx_open = False
            self.pending = []
            return None
        try:
            return self._commit_body(marker)
        except Exception:
            self.pending = []
            self._tx_open = False
            self.staged_rollback()
            self.ledger = load_ledger(self.root, code_version=self.code_version)
            self._refresh_ids()
            self.log_offset = self._disk_log_len()
            raise

    def _refuse_future_tail(self) -> None:
        """Do not append behind a log record kind this process cannot read.

        ``recover`` leaves such a tail unapplied. A record appended after it
        would put the unknown kind on the path to a durable commit, and the
        next open would refuse the whole log.
        """
        path = self.root / LOG_NAME
        if not path.exists():
            return
        for record in parse_log(path.read_bytes()):
            if record.is_unknown:
                raise UnavailableSchemaGap(
                    f"log holds {record.kind} at LSN {record.lsn}; this process will not append past it",
                    code_version=self.code_version,
                    stored_version=record.schema_version,
                    last_durable_lsn=self.ledger.applied_lsn,
                )

    def _commit_body(self, marker: str | None) -> int:
        self._refuse_future_tail()
        assert_same_volume(self.root / SNAP_NAME, lsn=0, stat_fn=self.stat_fn)
        self.log_offset = discard_torn_tail(self.root / LOG_NAME)
        tx_id = self._tx_id
        prev = 0
        staged: list[bytes] = []
        write_indexes: list[int] = []
        for before, after in self.pending:
            record = Record(
                lsn=self.next_lsn,
                prev_lsn=prev,
                tx_id=tx_id,
                kind="update",
                schema_version=self.ledger.schema_version,
                before=before,
                after=after,
            )
            self.next_lsn += 1
            prev = record.lsn
            staged.append(self._stage(record, "log_update"))
            write_indexes.append(self.last_log_write)  # type: ignore[arg-type]
        commit_after = {
            "account_id": None,
            "schema_version": self.ledger.schema_version,
            "element_states": dict(self.ledger.element_states),
        }
        if self.ledger.backfill_complete:
            commit_after["backfill_complete"] = True
        if self.ledger.cleanup_complete:
            commit_after["cleanup_complete"] = True
        if marker:
            commit_after["marker"] = marker
            commit_after[marker] = True
        commit_record = Record(
            lsn=self.next_lsn,
            prev_lsn=prev,
            tx_id=tx_id,
            kind="commit",
            schema_version=self.ledger.schema_version,
            before=None,
            after=commit_after,
        )
        self.next_lsn += 1
        staged.append(self._stage(commit_record, "log_commit"))
        write_indexes.append(self.last_log_write)  # type: ignore[arg-type]
        self.ledger.applied_lsn = commit_record.lsn
        if self.publish_before_log_flush:
            self._publish_current(depends_on=())
            self._force(staged, write_indexes, commit_record)
        else:
            flush = self._force(staged, write_indexes, commit_record)
            self._publish_current(depends_on=(flush,))
        self.pending = []
        self._tx_open = False
        return commit_record.lsn

    def staged_rollback(self) -> None:
        """Void log writes that never reached a force.

        Indexes stay put so later ``depends_on`` tuples still name the same
        operations. A flushed write is kept. Snapshot operations recorded
        after a successful publish are kept as well.
        """
        flushed: set[int] = set()
        for op in self.trace.ops:
            if op.role == "log_flush":
                flushed.update(op.covers)
        for op in self.trace.ops:
            if op.kind == "write" and op.path == LOG_NAME and op.index not in flushed:
                op.kind = "nop"
                op.data = b""
                op.role = "nop"
        self.last_log_write = None
        for op in self.trace.ops:
            if op.kind == "write" and op.path == LOG_NAME:
                self.last_log_write = op.index
        self.log_offset = self._disk_log_len()

    def durabilize_open_transaction(self) -> None:
        """Force the open updates and leave them without a commit record."""
        if not self._tx_open or not self.pending:
            raise ConstraintError("no open transaction")
        self._refuse_future_tail()
        self.log_offset = discard_torn_tail(self.root / LOG_NAME)
        tx_id = self._tx_id
        prev = 0
        staged: list[bytes] = []
        write_indexes: list[int] = []
        last = None
        for before, after in self.pending:
            record = Record(
                lsn=self.next_lsn,
                prev_lsn=prev,
                tx_id=tx_id,
                kind="update",
                schema_version=self.ledger.schema_version,
                before=before,
                after=after,
            )
            self.next_lsn += 1
            prev = record.lsn
            staged.append(self._stage(record, "log_update"))
            write_indexes.append(self.last_log_write)  # type: ignore[arg-type]
            last = record
        if last is None:
            raise ConstraintError("no open transaction")
        self._force(staged, write_indexes, last)
        self.pending = []
        self._tx_open = False

    def checkpoint(self, hint_lsn: int | None = None) -> None:
        """Append a checkpoint hint. It is not a commit and does not publish."""
        if self._tx_open:
            raise ConstraintError("checkpoint during an open transaction")
        self._load_idle()
        hint = self.ledger.applied_lsn if hint_lsn is None else hint_lsn
        self._refuse_future_tail()
        self.log_offset = discard_torn_tail(self.root / LOG_NAME)
        record = Record(
            lsn=self.next_lsn,
            prev_lsn=0,
            tx_id=0,
            kind="checkpoint",
            schema_version=self.ledger.schema_version,
            before=None,
            after={"hint_lsn": hint, "applied_lsn": self.ledger.applied_lsn},
        )
        self.next_lsn += 1
        blob = self._stage(record, "log_checkpoint")
        self._force([blob], [self.last_log_write], record)  # type: ignore[list-item]
        hint_path = self.root / "checkpoint.hint"
        hint_path.write_text(
            "{\"hint_lsn\": %d}\n" % hint,
            encoding="utf-8",
        )
        emit(
            "checkpoint",
            root=self.root,
            lsn=hint,
            schema_version=self.ledger.schema_version,
            path="checkpoint.hint",
            byte_count=hint_path.stat().st_size,
        )

    def transition(self, target: int, *, marker: str | None = None) -> None:
        """Commit one adjacent schema version. Caller fills rows before a gate."""
        if self._tx_open:
            raise ConstraintError("transition during an open transaction")
        self.ledger = load_ledger(self.root, code_version=self.code_version)
        assert_transition(self.ledger, target)
        current = self.ledger.schema_version
        before = {
            "account_id": None,
            "schema_version": current,
            "element_states": dict(self.ledger.element_states),
            "backfill_complete": self.ledger.backfill_complete,
            "cleanup_complete": self.ledger.cleanup_complete,
        }
        new_states = states_for(target)
        after = {
            "account_id": None,
            "schema_version": target,
            "element_states": new_states,
            "backfill_complete": self.ledger.backfill_complete or marker == "backfill_complete",
            "cleanup_complete": self.ledger.cleanup_complete or marker == "cleanup_complete",
        }
        if marker:
            after["marker"] = marker
        self._tx_open = True
        self._tx_id = self.next_tx
        self.next_tx += 1
        self.pending = [(before, after)]
        apply_image(self.ledger, after)
        self.commit(marker=marker)
        self.code_version = target
        emit(
            "migrate",
            root=self.root,
            schema_version=target,
            element="schema",
            element_state=marker or "step",
            lsn=self.ledger.applied_lsn,
        )

    def _stage(self, record: Record, role: str) -> bytes:
        blob = encode_record(record)
        deps = (self.last_log_write,) if self.last_log_write is not None else ()
        op = self.trace.add(
            kind="write",
            path=LOG_NAME,
            offset=self.log_offset,
            data=blob,
            depends_on=deps,
            role=role,
            commit_id=record.tx_id,
            lsn=record.lsn,
        )
        self.last_log_write = op.index
        self.log_offset += len(blob)
        emit(
            "log_append",
            root=self.root,
            commit_id=record.tx_id,
            lsn=record.lsn,
            schema_version=record.schema_version,
            path=LOG_NAME,
            byte_count=len(blob),
            element_state=record.kind,
        )
        return blob

    def _force(self, staged: list[bytes], write_indexes: list[int], tip: Record) -> int:
        path = self.root / LOG_NAME
        payload = b"".join(staged)
        discard_torn_tail(path)
        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        op = self.trace.add(
            kind="flush",
            path=LOG_NAME,
            depends_on=tuple(index for index in write_indexes if index is not None),
            covers=tuple(index for index in write_indexes if index is not None),
            role="log_flush",
            commit_id=tip.tx_id,
            lsn=tip.lsn,
        )
        emit(
            "log_flush",
            root=self.root,
            commit_id=tip.tx_id,
            lsn=tip.lsn,
            schema_version=tip.schema_version,
            path=LOG_NAME,
            byte_count=len(payload),
        )
        return op.index

    def _publish_current(self, *, depends_on: tuple[int, ...]) -> None:
        payload = encode_snapshot(self.ledger)
        publish_file(
            self.root / SNAP_NAME,
            payload,
            lsn=self.ledger.applied_lsn,
            stat_fn=self.stat_fn,
            replace_fn=self.replace_fn,
        )
        temp_name = f"snap-tmp-{self.ledger.applied_lsn}"
        write = self.trace.add(
            kind="write",
            path=temp_name,
            offset=0,
            data=payload,
            depends_on=depends_on,
            role="snapshot_write",
            commit_id=self._tx_id,
            lsn=self.ledger.applied_lsn,
        )
        flush = self.trace.add(
            kind="flush",
            path=temp_name,
            depends_on=(write.index,),
            covers=(write.index,),
            role="snapshot_flush",
            commit_id=self._tx_id,
            lsn=self.ledger.applied_lsn,
        )
        replace = self.trace.add(
            kind="replace",
            src=temp_name,
            dst=SNAP_NAME,
            depends_on=(flush.index,),
            role="replace",
            commit_id=self._tx_id,
            lsn=self.ledger.applied_lsn,
        )
        self.trace.add(
            kind="dir_flush",
            path=".",
            depends_on=(replace.index,),
            role="dir_flush",
            commit_id=self._tx_id,
            lsn=self.ledger.applied_lsn,
        )

    def _status_value(self, old: dict | None, requested: str | None) -> str | None:
        state = self._status()
        if state == "absent":
            return None
        if state == "delete_only":
            if old and old.get("status"):
                return old.get("status")
            return None
        if state in ("write_only", "public"):
            if requested:
                return requested
            if old and old.get("status"):
                return old.get("status")
            return "active"
        raise ConstraintError(state)

    def _index_after(self, old: dict | None, new_account: dict) -> tuple[dict, list]:
        state = self._email()
        old_email = old.get("email") if old else None
        new_email = new_account["email"]
        if state == "absent":
            return {}, []
        if state == "delete_only":
            clear = [old_email] if old_email and old_email != new_email else []
            return {}, clear
        if state in ("write_only", "public"):
            clear = [old_email] if old_email and old_email != new_email else []
            return {new_email: new_account["account_id"]}, clear
        raise ConstraintError(state)

    def _index_before(self, old: dict | None, after_set: dict) -> tuple[dict, list]:
        index_set: dict[str, int] = {}
        if old is not None:
            old_email = old["email"]
            if self.ledger.email_index.get(old_email) == old["account_id"]:
                index_set[old_email] = old["account_id"]
        index_clear = [email for email in after_set if email not in index_set]
        return index_set, index_clear

    @staticmethod
    def _check_row(account_id: int, balance: int, email: str) -> None:
        if type(account_id) is not int or account_id < 0:
            raise ConstraintError("account_id")
        if type(balance) is not int:
            raise ConstraintError("balance")
        if type(email) is not str or email.count("@") != 1 or email.startswith("@") or email.endswith("@"):
            raise ConstraintError("email")
        if any(ch.isspace() for ch in email):
            raise ConstraintError("email")
