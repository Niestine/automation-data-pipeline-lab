"""In-memory file store. Crash images are prefixes, plus one scheduled reorder.

No kernel calls. ``ordered_atomic`` keeps a prefix of issued operations, tears a
write larger than ``sector_size`` until that file is fsynced, and makes a
rename's directory entry durable only at directory fsync. ``relaxed`` can
surface a non-atomic rename before the directory barrier.

A directory is addressed two ways. An unpinned operation names the directory by
its path, which a concurrent ``move_parent`` can rebind to another directory.
A pinned operation uses the identity captured by ``pin_dir`` (the ``renameat``
model) and is not affected by the rebinding.
"""

from __future__ import annotations

from dataclasses import dataclass, field


PROFILES = ("ordered_atomic", "relaxed")


@dataclass(frozen=True)
class Op:
    kind: str
    name: str = ""
    data: bytes = b""
    src: str = ""
    dst: str = ""
    dir_id: int = 0
    pinned: bool = False
    target_dir: int = 0


@dataclass
class Image:
    durable: dict[int, dict[str, bytes]]
    unspecified: bool = False

    def bytes_at(self, dir_id: int, name: str) -> bytes | None:
        folder = self.durable.get(dir_id)
        if folder is None or name not in folder:
            return None
        return folder[name]

    def located_in(self, name: str) -> list[int]:
        return sorted(dir_id for dir_id, files in self.durable.items() if name in files)


@dataclass
class FakeFS:
    profile: str = "ordered_atomic"
    sector_size: int = 8
    initial: dict[int, dict[str, bytes]] = field(default_factory=dict)
    ops: list[Op] = field(default_factory=list)
    race_parent: bool = False
    race_target: int = 2
    _raced: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"unknown persistence profile: {self.profile!r}")
        if self.sector_size < 1:
            raise ValueError("sector_size must be positive")

    def mount(self, dir_id: int, files: dict[str, bytes]) -> None:
        if dir_id <= 0:
            raise ValueError("directory id must be positive")
        self.initial[dir_id] = {name: bytes(data) for name, data in files.items()}

    def pin_dir(self, dir_id: int) -> int:
        if dir_id not in self.initial:
            raise KeyError(dir_id)
        self.ops.append(Op("pin", dir_id=dir_id, pinned=True))
        return dir_id

    def write(self, name: str, data: bytes) -> None:
        self.ops.append(Op("write", name=name, data=bytes(data)))

    def fsync(self, name: str) -> None:
        self.ops.append(Op("fsync", name=name))

    def rename(self, src: str, dst: str, dir_id: int, *, pinned: bool) -> None:
        if self.race_parent and not self._raced:
            # A concurrent process renames a component of the parent path just
            # before this rename runs, so the path now names ``race_target``.
            self.move_parent(dir_id, self.race_target)
        self.ops.append(Op("rename", src=src, dst=dst, dir_id=dir_id, pinned=pinned))

    def fsync_dir(self, dir_id: int, *, pinned: bool = True) -> None:
        self.ops.append(Op("fsync_dir", dir_id=dir_id, pinned=pinned))

    def move_parent(self, dir_id: int, target_dir: int) -> None:
        if target_dir not in self.initial:
            raise KeyError(target_dir)
        self._raced = True
        self.ops.append(Op("move_parent", dir_id=dir_id, target_dir=target_dir))

    def mark_success(self) -> None:
        self.ops.append(Op("success"))


def torn(data: bytes, sector_size: int) -> bytes:
    """A sector-atomic write returns the full buffer. A larger write keeps the first sector."""

    if len(data) <= sector_size:
        return bytes(data)
    return bytes(data[:sector_size])


def mixed(old: bytes, new: bytes) -> bytes:
    """Deterministic garbage used when rename itself is not crash-atomic."""

    piece = b"MIX:" + new[:4] + b":" + old[:4]
    if piece in (old, new):
        piece += b"#torn"
    return piece


def replay(fs: FakeFS, count: int) -> Image:
    """Keep the first ``count`` issued operations and materialize the durable image."""

    if count < 0 or count > len(fs.ops):
        raise ValueError(f"cut {count} is outside 0..{len(fs.ops)}")
    return materialize(fs, fs.ops[:count])


def materialize(fs: FakeFS, persisted: list[Op]) -> Image:
    """Durable image after a crash that persisted exactly ``persisted``, in that order."""

    durable = {dir_id: dict(files) for dir_id, files in fs.initial.items()}
    volatile: dict[int, dict[str, bytes | None]] = {dir_id: {} for dir_id in durable}
    memory: dict[str, bytes] = {}
    synced: set[str] = set()
    binding: dict[int, int] = {}
    unspecified = False
    open_rename: tuple[int, str, str] | None = None
    for op in persisted:
        if op.kind in ("pin", "success"):
            continue
        if op.kind == "write":
            memory[op.name] = op.data
            synced.discard(op.name)
            continue
        if op.kind == "fsync":
            if op.name in memory:
                synced.add(op.name)
            continue
        if op.kind == "move_parent":
            binding[op.dir_id] = op.target_dir
            continue
        if op.kind == "rename":
            where = _resolve(op, binding)
            if where != op.dir_id:
                # POSIX: a path component changed in parallel with rename.
                unspecified = True
            content = _published_content(memory, synced, op.src, fs.sector_size)
            links = volatile.setdefault(where, {})
            links[op.dst] = content
            links[op.src] = None
            open_rename = (where, op.src, op.dst)
            continue
        if op.kind == "fsync_dir":
            where = _resolve(op, binding)
            _flush_dir(durable, volatile, where)
            if open_rename is not None and open_rename[0] == where:
                open_rename = None
            continue
        raise ValueError(f"unknown operation {op.kind}")
    if fs.profile == "relaxed" and open_rename is not None:
        # A rename that is not crash-atomic and not yet behind a directory
        # barrier can leave the destination holding a mix of old and new bytes.
        where, src, dst = open_rename
        old = durable.get(where, {}).get(dst, b"")
        new = _published_content(memory, synced, src, fs.sector_size)
        durable.setdefault(where, {})[dst] = mixed(old, new)
    return Image(durable=durable, unspecified=unspecified)


def reorder_rename_before_data_barrier(ops: list[Op]) -> tuple[list[Op], int]:
    """The one non-prefix crash: the rename and its directory fsync persist
    before the data fsync of the renamed file.

    Returns the reordered operation list and the cut just before the delayed
    data fsync.
    """

    rename_at = next((i for i, op in enumerate(ops) if op.kind == "rename"), None)
    if rename_at is None:
        raise ValueError("no rename to reorder")
    rename = ops[rename_at]
    barrier_at = next(
        (i for i in range(rename_at - 1, -1, -1) if ops[i].kind == "fsync" and ops[i].name == rename.src),
        None,
    )
    if barrier_at is None:
        raise ValueError("no data barrier precedes the rename")
    without = ops[:barrier_at] + ops[barrier_at + 1 :]
    rename_at -= 1
    dir_sync_at = next(
        (i for i in range(rename_at + 1, len(without)) if without[i].kind == "fsync_dir"),
        rename_at,
    )
    cut = dir_sync_at + 1
    reordered = without[:cut] + [ops[barrier_at]] + without[cut:]
    return reordered, cut


def _resolve(op: Op, binding: dict[int, int]) -> int:
    if op.pinned:
        return op.dir_id
    return binding.get(op.dir_id, op.dir_id)


def _published_content(memory: dict[str, bytes], synced: set[str], src: str, sector_size: int) -> bytes:
    if src not in memory:
        return b""
    if src in synced:
        return memory[src]
    return torn(memory[src], sector_size)


def _flush_dir(
    durable: dict[int, dict[str, bytes]],
    volatile: dict[int, dict[str, bytes | None]],
    dir_id: int,
) -> None:
    bucket = durable.setdefault(dir_id, {})
    for name, content in volatile.get(dir_id, {}).items():
        if content is None:
            bucket.pop(name, None)
        else:
            bucket[name] = content
    volatile[dir_id] = {}
