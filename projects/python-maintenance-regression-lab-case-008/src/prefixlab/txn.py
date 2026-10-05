"""Transactions: WAL append happens before the page changes. Undo follows UndoNxtLSN."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from prefixlab.pages import PageStore
from prefixlab.wal import LogRecord, WriteAheadLog

logger = logging.getLogger("prefixlab.txn")


@dataclass
class TxnState:
    txn: str
    last_lsn: int = 0
    state: str = "active"
    savepoints: list[int] = field(default_factory=list)


class Store:
    """In-memory page store plus its write-ahead log."""

    def __init__(self) -> None:
        self.log = WriteAheadLog()
        self.pages = PageStore()
        self.txns: dict[str, TxnState] = {}

    def open_page(self, page_id: str, data: bytes) -> None:
        self.pages.open_page(page_id, data)

    def page(self, page_id: str) -> bytes:
        return self.pages.get(page_id)

    def begin(self, txn: str) -> LogRecord:
        if not txn:
            raise ValueError("transaction id is required")
        if txn in self.txns:
            raise ValueError(f"transaction id {txn} was already used")
        record = self.log.append(txn=txn, kind="begin", prev_lsn=0)
        self.txns[txn] = TxnState(txn=txn, last_lsn=record.lsn, state="active")
        return record

    def update(self, txn: str, page_id: str, after: bytes) -> LogRecord:
        state = self._active(txn)
        if not isinstance(after, (bytes, bytearray)):
            raise ValueError("page image must be bytes")
        before = self.pages.get(page_id)
        record = self.log.append(
            txn=txn,
            kind="update",
            prev_lsn=state.last_lsn,
            page=page_id,
            before=before,
            after=bytes(after),
        )
        state.last_lsn = record.lsn
        self.pages.apply(record)
        return record

    def savepoint(self, txn: str) -> int:
        state = self._active(txn)
        record = self.log.append(txn=txn, kind="savepoint", prev_lsn=state.last_lsn)
        state.last_lsn = record.lsn
        state.savepoints.append(record.lsn)
        return record.lsn

    def rollback_to(self, txn: str, save_lsn: int | None = None, *, crash_after: int | None = None) -> bool:
        """Undo records newer than the savepoint. The transaction stays active.

        Returns False when ``crash_after`` stops the walker before it finishes.
        A chain CLR copies ``undo_nxt_lsn`` and does not compensate a CLR again.
        """

        state = self._active(txn)
        if save_lsn is None:
            if not state.savepoints:
                raise ValueError(f"transaction {txn} has no savepoint")
            save_lsn = state.savepoints[-1]
        if save_lsn not in state.savepoints:
            raise ValueError(f"savepoint {save_lsn} is not outstanding for {txn}")
        finished, _left = self.undo(txn, stop_lsn=save_lsn, budget=crash_after)
        if finished:
            state.savepoints = [point for point in state.savepoints if point < save_lsn]
        return finished

    def commit(self, txn: str) -> LogRecord:
        state = self._active(txn)
        record = self.log.append(txn=txn, kind="commit", prev_lsn=state.last_lsn)
        state.last_lsn = record.lsn
        state.state = "committed"
        self.log.force(record.lsn)
        return record

    def abort(self, txn: str) -> LogRecord:
        self._active(txn)
        self.undo(txn, stop_lsn=0, budget=None)
        return self.write_abort(txn)

    def write_abort(self, txn: str) -> LogRecord:
        state = self.txns[txn]
        record = self.log.append(txn=txn, kind="abort", prev_lsn=state.last_lsn)
        state.last_lsn = record.lsn
        state.state = "aborted"
        self.log.force(record.lsn)
        return record

    def _active(self, txn: str) -> TxnState:
        state = self.txns.get(txn)
        if state is None or state.state != "active":
            raise ValueError(f"transaction {txn} is not active")
        return state

    def undo(self, txn: str, stop_lsn: int, budget: int | None) -> tuple[bool, int | None]:
        """The one undo walker, shared by rollback-to, abort, and restart.

        Walks back from ``last_lsn`` until ``stop_lsn``. ``budget`` caps how many
        compensating CLRs are written before the walker stops as if crashed; it
        returns ``(finished, budget_left)``. A CLR met on the way is not undone
        again: the walker appends a page-less chain CLR that copies its
        ``undo_nxt_lsn`` and jumps there.
        """

        state = self.txns[txn]
        current = state.last_lsn
        steps = 0
        while current and current > stop_lsn:
            steps += 1
            if steps > 10000:
                raise RuntimeError(f"undo of {txn} did not advance")
            record = self.log.get(current)
            if record.kind == "clr":
                self._append_clr(txn, page="", before=b"", after=b"",
                                 undo_nxt_lsn=record.undo_nxt_lsn, compensates_lsn=0)
                current = record.undo_nxt_lsn
                continue
            if record.kind == "update":
                if budget is not None and budget <= 0:
                    return False, budget
                self._append_clr(
                    txn,
                    page=record.page,
                    before=self.pages.get(record.page),
                    after=record.before,
                    undo_nxt_lsn=record.prev_lsn,
                    compensates_lsn=record.lsn,
                )
                if budget is not None:
                    budget -= 1
                current = record.prev_lsn
                continue
            current = record.prev_lsn
        return True, budget

    def _append_clr(
        self,
        txn: str,
        *,
        page: str,
        before: bytes,
        after: bytes,
        undo_nxt_lsn: int,
        compensates_lsn: int,
    ) -> LogRecord:
        state = self.txns[txn]
        record = self.log.append(
            txn=txn,
            kind="clr",
            prev_lsn=state.last_lsn,
            page=page,
            before=before,
            after=after,
            undo_nxt_lsn=undo_nxt_lsn,
            compensates_lsn=compensates_lsn,
        )
        state.last_lsn = record.lsn
        self.pages.apply(record)
        self.log.force(record.lsn)
        return record


def apply_script(store: Store, script: dict) -> Store:
    """Run a JSON script of begin/update/savepoint/rollback/commit/abort steps."""

    if not isinstance(script, dict):
        raise ValueError("script must be an object")
    pages = script.get("pages")
    steps = script.get("steps")
    if not isinstance(pages, dict) or not isinstance(steps, list):
        raise ValueError("script requires pages and steps")
    for page_id, text in pages.items():
        if not isinstance(page_id, str) or not isinstance(text, str):
            raise ValueError("page images must be strings")
        store.open_page(page_id, text.encode("utf-8"))
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"step {index} must be an object")
        op = step.get("op")
        txn = step.get("txn")
        if not isinstance(txn, str) or not txn:
            raise ValueError(f"step {index} requires a transaction id")
        if op == "begin":
            store.begin(txn)
        elif op == "update":
            page_id = step.get("page")
            after = step.get("after")
            if not isinstance(page_id, str) or not isinstance(after, str):
                raise ValueError(f"step {index} update requires page and after")
            store.update(txn, page_id, after.encode("utf-8"))
        elif op == "savepoint":
            store.savepoint(txn)
        elif op == "rollback":
            store.rollback_to(txn)
        elif op == "commit":
            store.commit(txn)
        elif op == "abort":
            store.abort(txn)
        else:
            raise ValueError(f"step {index} has unknown op {op!r}")
    return store
