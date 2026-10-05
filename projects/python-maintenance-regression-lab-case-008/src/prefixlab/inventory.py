"""Stock decrement. The unlocked read-modify-write pays twice; the write lock does not.

There is one function, ``decrement_if_enough``, written as a generator that
yields at ``after_read`` and ``after_write`` (and at ``blocked`` while it waits
for the row lock). A deterministic scheduler steps the calls, so the failing
interleaving is a script, not a race against a clock.

The planted bug is a split write. ``consumed`` is incremented on the live row,
so every accepted call records a payout. ``remaining`` is a blind write of the
snapshot minus the amount, so a second call that saw the same snapshot replaces
the first deduction instead of stacking it. ``tokens`` are written from the
snapshot list, so the first committed token disappears.

Original 5 and two decrements of 3 therefore end at remaining 2 and consumed 6
unless the read holds a write lock until commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Generator

from prefixlab.errors import OracleFault

logger = logging.getLogger("prefixlab.inventory")

MODES = ("nolock", "txn_read_committed", "exclusive")


@dataclass
class Stock:
    remaining: int
    consumed: int
    original: int
    tokens: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.remaining + self.consumed


@dataclass
class Call:
    call_id: str
    accepted: bool
    status: str
    snap_tokens: list[str]
    snap_remaining: int
    token: str | None
    read_done: bool


@dataclass
class Run:
    stock: Stock
    calls: list[Call]
    events: list[str]
    mode: str
    schedule: str

    def history(self) -> dict:
        transactions = []
        for call in self.calls:
            ops = []
            if call.read_done:
                ops.append({"op": "read", "key": "stock", "value": list(call.snap_tokens)})
            if call.token is not None:
                ops.append(
                    {
                        "op": "append",
                        "key": "stock",
                        "tokens": [call.token],
                        "value": list(call.snap_tokens) + [call.token],
                    }
                )
            if not ops:
                continue
            transactions.append({"id": call.call_id, "status": call.status, "ops": ops})
        return {
            "transactions": transactions,
            "surviving": {"stock": list(self.stock.tokens)},
        }


class Shelf:
    """One stock row plus an in-process row lock. Not evidence about any SQL engine."""

    def __init__(self, original: int) -> None:
        if original < 0:
            raise ValueError("original stock must be non-negative")
        self.stock = Stock(remaining=original, consumed=0, original=original, tokens=[])
        self.lock_holder: str | None = None
        self.events: list[str] = []

    def try_lock(self, call_id: str) -> bool:
        if self.lock_holder not in (None, call_id):
            return False
        self.lock_holder = call_id
        return True

    def unlock(self, call_id: str) -> None:
        if self.lock_holder != call_id:
            raise RuntimeError(f"{call_id} does not hold the stock lock")
        self.lock_holder = None

    def try_read(self) -> dict:
        """A reader that respects the row lock, as a second exclusive caller would."""

        if self.lock_holder is not None:
            return {"blocked": True, "tokens": None, "remaining": None, "consumed": None}
        return {
            "blocked": False,
            "tokens": list(self.stock.tokens),
            "remaining": self.stock.remaining,
            "consumed": self.stock.consumed,
        }

    def write(self, snap_remaining: int, snap_tokens: list[str], amount: int, token: str) -> None:
        self.stock.remaining = snap_remaining - amount
        self.stock.consumed = self.stock.consumed + amount
        self.stock.tokens = list(snap_tokens) + [token]

    def image(self) -> tuple[int, int, list[str]]:
        return self.stock.remaining, self.stock.consumed, list(self.stock.tokens)

    def restore(self, image: tuple[int, int, list[str]]) -> None:
        self.stock.remaining, self.stock.consumed, tokens = image
        self.stock.tokens = list(tokens)


def decrement_if_enough(
    shelf: Shelf, call_id: str, amount: int, mode: str, *, abort: bool = False
) -> Generator[str, None, Call]:
    """Read remaining; if it covers ``amount``, write the decrement and commit.

    ``nolock`` writes straight to the row. ``txn_read_committed`` buffers its
    write until commit, so nobody sees it early, but its read takes no lock.
    ``exclusive`` takes the row lock before the read and holds it until commit
    or abort. ``abort`` stops after the write yield and rolls the call back.
    """

    _check_mode(mode)
    _check_amount(amount)
    if abort and mode == "nolock":
        raise ValueError("nolock has no transaction to abort")
    return _decrement(shelf, call_id, amount, mode, abort)


def _decrement(shelf: Shelf, call_id: str, amount: int, mode: str, abort: bool) -> Generator[str, None, Call]:
    events = shelf.events
    if mode == "exclusive":
        while not shelf.try_lock(call_id):
            events.append(f"{call_id}.blocked")
            yield "blocked"
        events.append(f"{call_id}.lock")
    elif mode == "txn_read_committed":
        events.append(f"{call_id}.begin")

    snap_remaining, _consumed, snap_tokens = shelf.image()
    events.append(f"{call_id}.after_read")
    yield "after_read"

    if snap_remaining < amount:
        events.append(f"{call_id}.reject")
        _finish(shelf, call_id, mode, "commit")
        logger.info("call=%s mode=%s event=reject", call_id, mode)
        return Call(call_id, False, "committed", snap_tokens, snap_remaining, None, True)

    token = f"{call_id}-tok"
    undo_image = shelf.image()
    if mode != "txn_read_committed":
        shelf.write(snap_remaining, snap_tokens, amount, token)
    events.append(f"{call_id}.after_write")
    yield "after_write"

    if abort:
        if mode == "exclusive":
            shelf.restore(undo_image)
        _finish(shelf, call_id, mode, "abort")
        logger.info("call=%s mode=%s event=abort", call_id, mode)
        return Call(call_id, False, "aborted", snap_tokens, snap_remaining, token, True)
    if mode == "txn_read_committed":
        shelf.write(snap_remaining, snap_tokens, amount, token)
    _finish(shelf, call_id, mode, "commit")
    logger.info("call=%s mode=%s event=accept", call_id, mode)
    return Call(call_id, True, "committed", snap_tokens, snap_remaining, token, True)


def _finish(shelf: Shelf, call_id: str, mode: str, how: str) -> None:
    if mode != "nolock":
        shelf.events.append(f"{call_id}.{how}")
    if mode == "exclusive":
        shelf.unlock(call_id)
        shelf.events.append(f"{call_id}.unlock")


def drive(shelf: Shelf, calls: dict[str, Generator[str, None, Call]], steps: list[str]) -> list[Call]:
    """Advance each named call by one yield, in ``steps`` order, then run all to completion.

    A call that yields ``blocked`` is retried on its next turn. If every unfinished
    call is blocked for a whole round, the schedule is a deadlock.
    """

    results: dict[str, Call] = {}

    def step(call_id: str) -> str | None:
        try:
            return next(calls[call_id])
        except StopIteration as stop:
            results[call_id] = stop.value
            return None

    for call_id in steps:
        if call_id not in results:
            step(call_id)
    pending = [call_id for call_id in calls if call_id not in results]
    while pending:
        progressed = False
        for call_id in pending:
            while call_id not in results:
                if step(call_id) == "blocked":
                    break
                progressed = True
        pending = [call_id for call_id in calls if call_id not in results]
        if pending and not progressed:
            raise RuntimeError(f"schedule deadlocked on {pending}")
    return [results[call_id] for call_id in calls]


def run_serial(mode: str, original: int = 5, amount: int = 3) -> Run:
    shelf = Shelf(original)
    calls = {cid: decrement_if_enough(shelf, cid, amount, mode) for cid in ("c1", "c2")}
    first = drive(shelf, {"c1": calls["c1"]}, [])
    second = drive(shelf, {"c2": calls["c2"]}, [])
    return Run(shelf.stock, first + second, shelf.events, mode, "serial")


def run_both_reads_first(mode: str, original: int = 5, amount: int = 3) -> Run:
    """Step c1 to its read yield, then c2 to its read yield, then finish both.

    Under ``exclusive`` the c2 step blocks on the lock instead of reading.
    """

    shelf = Shelf(original)
    calls = {cid: decrement_if_enough(shelf, cid, amount, mode) for cid in ("c1", "c2")}
    results = drive(shelf, calls, ["c1", "c2"])
    return Run(shelf.stock, results, shelf.events, mode, "both_reads_first")


def run_exclusive_abort(original: int = 5, amount: int = 3) -> tuple[Run, dict, dict]:
    """Write under the lock, show that an outside read is blocked, then abort."""

    shelf = Shelf(original)
    call = decrement_if_enough(shelf, "c1", amount, "exclusive", abort=True)
    if next(call, None) != "after_read" or next(call, None) != "after_write":
        raise RuntimeError("abort fixture requires enough stock")
    while_locked = shelf.try_read()
    holder = {
        "tokens": list(shelf.stock.tokens),
        "remaining": shelf.stock.remaining,
        "consumed": shelf.stock.consumed,
    }
    (result,) = drive(shelf, {"c1": call}, [])
    after = shelf.try_read()
    run = Run(shelf.stock, [result], shelf.events, "exclusive", "abort_after_write")
    return run, while_locked, {"holder": holder, "after": after}


def fault_if_lost(run: Run) -> dict:
    """Raise when a schedule loses a token or builds a dependency cycle."""

    from prefixlab.history import check_history

    report = check_history(run.history())
    cycle = report["anomaly"] in ("G0", "G1c", "G2")
    if not cycle and not report["missing"]:
        return report
    payload = {
        "cut": run.schedule,
        "outcome": report["anomaly"] or "lost_append",
        "transactions": list(report["transactions"]),
        "missing": list(report["missing"]),
    }
    logger.error(
        "outcome=%s cut=%s transactions=%s",
        payload["outcome"],
        payload["cut"],
        ",".join(payload["transactions"]),
    )
    raise OracleFault(payload)


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown lock mode: {mode!r}")


def _check_amount(amount: int) -> None:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("amount must be a positive integer")
