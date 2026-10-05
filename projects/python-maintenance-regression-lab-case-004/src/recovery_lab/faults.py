"""Model Z cuts and Model P images over an in-memory operation trace.

Model Z follows the clean power-fault rule: a completed flush is kept, an
interrupted write contributes nothing, and a later operation is dropped when
it depends on a dropped one. Block size selects which write offsets are cut
points. It does not discard a finished flush just because the file is shorter
than the block.

Model P is built by the atomicity tests, not by ``materialize``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recovery_lab.format import LOG_NAME, SNAP_NAME, intact_log_bytes


@dataclass
class Op:
    index: int
    kind: str
    path: str = ""
    offset: int = 0
    data: bytes = b""
    src: str = ""
    dst: str = ""
    depends_on: tuple[int, ...] = ()
    covers: tuple[int, ...] = ()
    role: str = ""
    commit_id: int = 0
    lsn: int = 0


@dataclass
class Trace:
    ops: list[Op] = field(default_factory=list)

    def add(self, **kwargs: object) -> Op:
        op = Op(index=len(self.ops), **kwargs)  # type: ignore[arg-type]
        self.ops.append(op)
        return op


@dataclass(frozen=True)
class Cut:
    op_index: int
    offset: int
    block: int


def dropped_ops(trace: list[Op], cut: Cut) -> set[int]:
    dropped = {cut.op_index}
    changed = True
    while changed:
        changed = False
        for op in trace:
            if op.index in dropped or op.index <= cut.op_index:
                continue
            if any(dep in dropped for dep in op.depends_on):
                dropped.add(op.index)
                changed = True
    return dropped


def block_cuts(trace: list[Op], block: int) -> list[Cut]:
    cuts: list[Cut] = []
    for op in trace:
        if op.kind != "write" or not op.data:
            continue
        offset = 0
        while offset < len(op.data):
            cuts.append(Cut(op_index=op.index, offset=offset, block=block))
            offset += block
    return cuts


def _stream_offset(trace: list[Op], cut: Cut) -> int:
    total = 0
    for op in trace:
        if op.kind != "write":
            continue
        if op.index == cut.op_index:
            return total + cut.offset
        total += len(op.data)
    return total


def score_cut(trace: list[Op], cut: Cut) -> int:
    """Commit-record bytes 100, a write between a log flush and the next
    replace 80, update payload 40, anything else 0.

    Cuts are only taken inside writes, so a replace is never scored itself.
    """
    op = trace[cut.op_index]
    score = 0
    if op.role == "log_commit" and cut.offset < len(op.data):
        score = max(score, 100)
    if _between_flush_and_replace(trace, cut.op_index):
        score = max(score, 80)
    if op.role == "log_update" and cut.offset < len(op.data):
        score = max(score, 40)
    return score


def _between_flush_and_replace(trace: list[Op], op_index: int) -> bool:
    flushes = [op.index for op in trace if op.role == "log_flush"]
    replaces = [op.index for op in trace if op.kind == "replace"]
    for flush_index in flushes:
        later = [index for index in replaces if index > flush_index]
        if later and flush_index < op_index < later[0]:
            return True
    return False


def rank_cuts(trace: list[Op], cuts: list[Cut]) -> list[Cut]:
    return sorted(cuts, key=lambda cut: (-score_cut(trace, cut), _stream_offset(trace, cut), cut.op_index))


def materialize(trace: list[Op], cut: Cut | None) -> dict[str, bytes]:
    """Replay ``trace`` into durable file bytes.

    ``cut is None`` keeps every operation. A cut drops that operation and
    every later operation that depends on a dropped one. Interrupted writes
    contribute no bytes: their flush depends on them, so it is dropped too.
    """
    dropped: set[int] = set()
    if cut is not None:
        dropped = dropped_ops(trace, cut)
    volatile: dict[str, bytes] = {}
    durable: dict[str, bytes] = {}
    published: dict[str, bytes] = {}
    pending: tuple[str, bytes] | None = None

    for op in trace:
        if op.index in dropped:
            continue
        if cut is not None and op.index == cut.op_index:
            continue
        if op.kind == "write":
            volatile[op.path] = _splice(volatile.get(op.path, b""), op.offset, op.data)
        elif op.kind == "flush":
            durable[op.path] = volatile.get(op.path, durable.get(op.path, b""))
            volatile[op.path] = durable[op.path]
        elif op.kind == "replace":
            pending = (op.dst, durable.get(op.src, b""))
        elif op.kind == "dir_flush" and pending is not None:
            dest, payload = pending
            published[dest] = payload
            durable[dest] = payload
            volatile[dest] = payload
            pending = None

    files: dict[str, bytes] = {}
    if LOG_NAME in durable:
        files[LOG_NAME] = intact_log_bytes(durable[LOG_NAME])
    snap = published.get(SNAP_NAME, b"")
    if snap:
        files[SNAP_NAME] = snap
    return files


def _splice(existing: bytes, offset: int, data: bytes) -> bytes:
    buf = bytearray(existing)
    end = offset + len(data)
    if end > len(buf):
        buf.extend(b"\x00" * (end - len(buf)))
    buf[offset:end] = data
    return bytes(buf)


def write_image(directory, files: dict[str, bytes]) -> None:
    from pathlib import Path

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for name in (LOG_NAME, SNAP_NAME):
        path = root / name
        blob = files.get(name)
        if blob:
            path.write_bytes(blob)
        elif path.exists():
            path.unlink()


def first_failure_rank(classes: list[str]) -> int | None:
    """1-based rank of the first data_loss or mixed_fields result."""
    for index, outcome in enumerate(classes, start=1):
        if outcome in ("data_loss", "mixed_fields"):
            return index
    return None
