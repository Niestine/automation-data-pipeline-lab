# Python Maintenance & Regression Lab — Case 008

A shelf file and one stock row, updated under a write-ahead log. The maintenance work is the crash and concurrency oracles: every stable log prefix, every publish barrier, and two overlapping decrements.

The subject is synthetic. Page images are `base-A`, `base-B`, and `base-C`. The shelf file starts as the bytes in `examples/shelf_bytes.json`. Nothing in this tree calls a live service, SQLite, or the kernel.

## Problem

A process can stop between a page update and its commit record, between a savepoint rollback and the next commit, or between the steps of a file replace. A later reader has to tell those cases apart:

- a committed page image is present only when its commit record is inside the stable prefix
- an uncommitted transaction is absent at every prefix, including a prefix taken in the middle of a savepoint rollback
- a careful file replace leaves the published name holding either the previous bytes or the full new bytes
- rename before the data fsync can publish a torn file, and returning success before the directory fsync can report success while the name is still old
- two `decrement if enough` calls that both read the stock before either writes pay out twice unless the read holds a write lock until commit

The suite is the maintenance contract for those distinctions.

## Architecture

`src/prefixlab` is a standard-library package. The store never opens a file and never starts a thread.

| Piece | Role |
| --- | --- |
| `wal.py` | Monotonic LSN, volatile tail, force watermark, length-prefixed frames |
| `pages.py` | Page bytes plus `page_lsn`. Redo skips a record the page already reflects |
| `txn.py` | Begin, update, savepoint, rollback-to, commit, abort |
| `recover.py` | Redo every stable record, then undo transactions with no commit or abort |
| `fakefs.py` | In-memory write, fsync, rename, directory fsync, a parent-path race, and one scheduled reorder |
| `publish.py` | Safe replace, plus the two unsafe orders used only by tests |
| `history.py` | Append-only lists and an Adya cycle check |
| `inventory.py` | One `decrement_if_enough` generator under `nolock`, `txn_read_committed`, and `exclusive`, plus a deterministic scheduler |

On a crash the unforced log tail is dropped. Recovery rebuilds pages from their original bytes and the stable prefix. It does not read the audit log.

The fake store classifies a published name as `old`, `new`, `corrupt`, `durability_loss`, or `unspecified`. `ordered_atomic` treats a prefix of issued operations as the crash image, tears a write larger than `sector_size` until that file is fsynced, and makes a renamed directory entry durable only at directory fsync. `relaxed` can leave a mix of old and new bytes when rename itself is not crash-atomic.

## Run

From the repository root:

```text
python -m unittest discover -s projects/python-maintenance-regression-lab-case-008/tests -v
```

From this directory:

```text
python run_lab.py metrics
python run_lab.py demo
```

`metrics` prints the oracle counts as JSON: recovery mismatches, safe-publish corrupt cuts, the reordered rename, the locked and unlocked stock sums, and the checker result for each fixture in `examples/histories.json`. `demo` prints one line with the recovery mismatch count, the safe-publish corrupt count, and the reorder outcome. Both stay offline.

The suite was run on CPython 3.10 and 3.13. It uses only the standard library.

## Design decisions

**The log record is appended before the page changes.** Commit forces the log through the commit LSN. A background force of an update without its commit record still rolls that update back, because recovery undoes any transaction that has no commit record.

**A savepoint is the transaction's current LSN.** Rollback undoes only later records and leaves the transaction active. Each undone update appends one compensation record whose `undo_nxt_lsn` is that update's `prev_lsn`, then installs the before-image. When the walker meets a compensation record, it appends a page-less chain record that copies `undo_nxt_lsn` and does not count as a second compensation. Rollback-to, abort, and restart all call the same walker (`Store.undo`). A crash during that walk, and a second crash during restart undo, still compensate each forward LSN at most once. A transaction that rolled back to a savepoint and then crashed without committing disappears entirely, including the writes from before the savepoint. After a finished restart, a second restart appends nothing.

**Repeat history, then undo losers.** Restart replays updates and compensation records, applying a record only when `page_lsn` is older than the record LSN. Commit and abort records only update the transaction table. Transactions that already have an abort record are not undone again.

**The safe replace is four barriers.** Pin the parent directory, write the full new bytes to a temp name, fsync the temp, rename onto the destination with that directory id, fsync the directory, then return success. Under `ordered_atomic` every prefix is `old` or `new`. The tests also run rename-before-data-fsync, success-before-directory-fsync, and the same safe sequence under `relaxed`. The scheduled reorder takes the safe protocol's own issued operations and persists the rename and directory fsync ahead of the data fsync; that image is `corrupt`, while every prefix of the same operations is not. The relaxed corrupt cut is an assumption failure of the protocol, not a success. For the directory race, the fake store rebinds the parent path to a second directory just before the rename. An unpinned publish resolves the path at rename time, so its entry lands in the other directory and the cut is `unspecified`. A pinned publish uses the captured directory identity and stays in the original directory.

**The stock bug is a split write.** `consumed` is incremented on the live row, so both accepted calls record a payout. `remaining` is a blind write of the snapshot minus the amount, so the second call replaces the first deduction. The token list is written from the snapshot, so the first committed token is absent from the surviving list. Original stock 5 and two decrements of 3 end at sum 8 on `nolock` and on `txn_read_committed`. `txn_read_committed` buffers its write until commit, so no one reads it early, but its read takes no lock. `exclusive` holds a write lock from the read until commit or abort, so the second call yields `blocked` until the first commits, its read sees the updated remaining, one call rejects, and the sum stays 5. All three modes run the same generator, which yields at `after_read` and `after_write`; the scheduler steps both calls to their read before finishing either. Nothing sleeps.

**The checker does not search for a serial order.** Each checked key is an append-only list of tokens. Version order is prefix order of those lists. The checker emits write-depends, read-depends, and anti-depends edges and reports one cycle as `G0`, `G1c`, or `G2`. It searches write-depends edges first, then adds read-depends, then anti-depends, so an extra anti-depends edge cannot turn a `G0` cycle into a `G2` report. A committed read of an aborted token is `G1a`. A read of a proper prefix of a writer's final list is `G1b`. A committed token missing from the surviving list is `lost_append` when no cycle exists. A transaction whose commit was not acknowledged is `unknown`: it is not an aborted read and it is not a committed install.

Failed publish and stock oracles raise `OracleFault` with `cut` or the schedule name, `outcome`, and `transactions`. The log line carries the same outcome and cut.

## Limitations

Fuzzy checkpoints, media recovery, and semantic increment locks are out of scope. The fake store uses prefix cuts plus one scheduled reorder, not a search over syscall permutations. Predicate anomalies are out of scope, and two committed lists on one key that are not prefixes of each other (an incompatible version order) are not reported as their own anomaly; the lab relies on the cycle and `missing` fields for that run. `sector_size` is a parameter (8 in the fixtures) so a short tear is visible; this lab does not claim those filesystem measurements were reproduced. The write lock is in-process. It is not evidence about SQLite's `BEGIN IMMEDIATE`. There is no network, no clock, and no real disk.

## What it demonstrates

- Write-ahead recovery at every stable prefix of a committed update, an uncommitted update, and a savepoint that later commits
- Compensation of each forward update at most once across a nested rollback and a crash during restart undo
- Idempotent redo when `page_lsn` already reflects the record, and a torn final log frame dropped before replay
- Old, new, corrupt, durability-loss, and unspecified publish outcomes, including the profile where rename is not atomic. The two persistence profiles are the compatibility surface: the same protocol is checked against both
- A directory pin that keeps a racing parent rename from making the publish unspecified
- The stock invariant `remaining + consumed == original` under a write lock, and the sum 8 when the same interleaving does not take that lock
- Named isolation anomalies from client-visible append lists, including an aborted token a dirty reader was allowed to see
