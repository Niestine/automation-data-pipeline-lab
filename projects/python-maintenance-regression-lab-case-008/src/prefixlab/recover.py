"""Restart: redo every stable record, then undo transactions that never committed."""

from __future__ import annotations

import logging

from prefixlab.txn import Store, TxnState
from prefixlab.wal import LogRecord, format_record

logger = logging.getLogger("prefixlab.recover")


def restart(
    initial: dict[str, bytes],
    records: list[LogRecord],
    *,
    crash_after: int | None = None,
) -> Store:
    """Rebuild pages from ``initial`` and a stable record prefix.

    ``crash_after`` stops loser undo after that many new compensating CLRs
    and does not write the abort record. A second call with the returned
    log finishes the undo. Compensation counts do not increase for an LSN
    that was already compensated.
    """

    store = Store()
    for page_id, data in initial.items():
        store.open_page(page_id, data)
    store.log.adopt(list(records))
    _redo(store)
    _undo_losers(store, crash_after)
    return store


def _redo(store: Store) -> None:
    for record in store.log.records:
        logger.info(format_record(record))
        _note(store, record)
        store.pages.apply(record)


def _note(store: Store, record: LogRecord) -> None:
    if record.kind == "begin":
        store.txns[record.txn] = TxnState(txn=record.txn, last_lsn=record.lsn, state="active")
        return
    state = store.txns.get(record.txn)
    if state is None:
        return
    state.last_lsn = record.lsn
    if record.kind == "savepoint":
        state.savepoints.append(record.lsn)
    elif record.kind == "commit":
        state.state = "committed"
    elif record.kind == "abort":
        state.state = "aborted"


def _undo_losers(store: Store, crash_after: int | None) -> None:
    budget = crash_after
    for txn in sorted(store.txns):
        if store.txns[txn].state != "active":
            continue
        finished, budget = store.undo(txn, stop_lsn=0, budget=budget)
        if not finished:
            return
        store.write_abort(txn)

