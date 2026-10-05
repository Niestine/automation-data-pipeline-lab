# Research application

This lab uses five public sources. The tests are the check that each technique is actually in the code.

## Write-ahead recovery and partial rollback

Mohan, Haderle, Lindsay, Pirahesh, and Schwarz, "ARIES: A Transaction Recovery Method Supporting Fine-Granularity Locking and Partial Rollbacks Using Write-Ahead Logging," ACM Transactions on Database Systems, 1992.
https://web.stanford.edu/class/cs345d-01/rl/aries.pdf

The log gives every record a monotonic LSN. Updates sit in a volatile buffer until `force` advances a watermark; a crash drops the unforced tail, and a short final frame is not part of the stable prefix. An update record stores the before-image and the after-image. The page stores `page_lsn`, and redo installs a record only when that LSN is older, which is what makes a second replay leave the bytes unchanged.

Restart redoes every stable record, including compensation records, and only then undoes transactions that have no commit record. A savepoint remembers the transaction's current LSN. Rollback undoes only newer updates and leaves the transaction active. Each undone update appends a compensation record whose `undo_nxt_lsn` is the undone record's `prev_lsn`. When the walker meets a compensation record it does not compensate that update again: it appends a page-less chain record that copies `undo_nxt_lsn` and moves to that LSN. Rollback-to, abort, and restart share this one walker. The paper's bound is the test: across a nested rollback, a crash in that rollback, and a second crash during restart undo, each forward LSN is compensated at most once. A finished restart appends nothing on the next restart. A transaction that reached a savepoint and never committed is a loser, so the pre-savepoint writes disappear too.

Fuzzy checkpoints, media recovery, and operation logging are named in the paper and are not implemented. The lab log is replayed from the first record.

## Crash cuts on a fake file store

Pillai, Chidambaram, Alagappan, Al-Kiswany, Arpaci-Dusseau, and Arpaci-Dusseau, "All File Systems Are Not Created Equal: On the Complexity of Crafting Crash-Consistent Applications," OSDI 2014.
https://www.usenix.org/conference/osdi14/technical-sessions/presentation/pillai

The store is in-memory. Its operations are write, file fsync, rename, and directory fsync. A crash keeps a prefix of those operations, or the one scheduled reorder: the safe protocol's own issued operations with the rename and directory fsync persisted ahead of the data fsync. Outcomes are `old`, `new`, or `corrupt` (empty, torn, or any other bytes). A write no larger than `sector_size` is all-or-nothing; a larger write can tear before that file's fsync. The fixture sector size is 8, which is a parameter, not a measured device.

The safe replace fsyncs the temp file before rename and fsyncs the directory before returning success. Under the ordered, atomic-rename profile every prefix is old or new, and every cut after success is new. Rename before the data fsync has a corrupt cut. Returning success before the directory fsync has a durability-loss cut: success was returned and the published name is still old. The relaxed profile, where rename need not be atomic, has a corrupt cut on the same safe sequence. That cut is recorded as a failed assumption. This lab does not re-find the paper's application bugs and does not claim the ext3, ext4, or btrfs measurements were reproduced.

## Directory identity for rename

POSIX.1-2017, `rename` / `renameat`, The Open Group Base Specifications Issue 7.
https://pubs.opengroup.org/onlinepubs/9699919799/functions/rename.html

A path component that changes in parallel with `rename` is unspecified. Opening the directory and renaming against that identity keeps the file in the directory that was opened. The fake store models this as a pin. The race rebinds the parent path to a second directory just before the rename. An unpinned publish resolves the path at rename time, so its entry lands in the second directory and the cut is `unspecified`. A pinned publish of the same race uses the captured identity, leaves the file in the original directory, and the bytes are old or new. The lab does not call the kernel.

## Append-only histories and dependency cycles

Kingsbury and Alvaro, "Elle: Inferring Isolation Anomalies from Experimental Observations," arXiv:2003.10554, 2020.
https://arxiv.org/abs/2003.10554

Checked keys are append-only lists of unique tokens. A read returns the whole list. Version order is prefix order of those lists, not commit time. From the lists the checker builds write-depends, read-depends, and anti-depends edges. It reports `G1a` for a committed read of a token whose writer aborted, `G1b` for a read of a proper prefix of a writer's final list, `G0` for a cycle of only write-depends edges, `G1c` for a cycle that includes a read-depends edge and no anti-depends edge, and `G2` for a cycle with an anti-depends edge. Cycles are searched over write-depends edges first, then read-depends, then anti-depends, so the report names the weakest edge set that already forms a cycle. One cycle is the counterexample: anomaly code, one edge per hop, and transaction ids. A transaction with status `unknown` adds no `G1a` edge and is not a committed install. Predicate anomalies are out of scope. The hand-built histories in `examples/histories.json` are the fixtures.

## Exclusive read-modify-write on one stock row

Warszawski and Bailis, "ACIDRain: Concurrency-Related Attacks on Database-Backed Web Applications," SIGMOD 2017.
https://www.bailis.org/papers/acidrain-sigmod2017.pdf

The planted bug is the paper's withdraw fragment. A call reads the remaining stock, accepts when it covers the amount, then writes the difference. Two overlapping calls can both pass the check. Wrapping the same body in begin and commit still loses the update when the read does not take a write lock. The fix in this lab is an in-process write lock held from the read until commit or abort. The SQLite mode named in some discussions of the same bug was not used; this lock is not evidence about SQLite.

The invariant is `remaining + consumed == original`. Original stock is 5 and each call decrements by 3. Serial execution accepts one call and keeps the sum at 5. The both-reads-first schedule accepts both calls and moves the sum to 8 under `nolock` and under `txn_read_committed`. The same schedule under the write lock accepts one call and keeps the sum at 5. All three modes run one generator, `decrement_if_enough`, that yields at `after_read` and `after_write` (and `blocked` while waiting for the lock); a deterministic scheduler steps both calls to their read before finishing either. `txn_read_committed` buffers its write until commit but takes no lock on the read. Abort after the write yield restores the row, and a reader blocked on the lock does not observe the aborted token. If a reader is handed that token anyway, the checker reports `G1a`.
