# Python Maintenance & Regression Lab — Case 004

A small account ledger with a checksummed write-ahead log and a published snapshot. The maintenance work is the recovery path: torn tails, checksum holes, a snapshot that lands before its commit record is durable, and a schema change that moves one element by one state at a time.

The subject is synthetic. Account 1 starts at balance 10 with `a@example.com`, then moves to balance 25. Nothing in this tree calls a live service.

## Problem

A process can stop between appending a commit record, flushing that record, writing a snapshot, and publishing the snapshot name. A later reader has to tell those cases apart:

- the newest commit whose record is fully flushed is the recovered ledger
- a checksum hole inside a flushed prefix is structural corruption, and the files stay as they were
- a snapshot whose stored schema is more than one version away from this process is refused, and the files stay as they were
- an index or a required column introduced in one step can leave an orphan key or a row with no status when two versions run together

The lab fixes that reader. Regression tests build the crash images and score each one as `durable_match`, `data_loss`, `mixed_fields`, `structural_corruption`, `refused_but_recoverable`, or `unavailable_gap`.

## Architecture

`src/recovery_lab` is a standard-library package.

| Piece | Role |
| --- | --- |
| `format.py` | Canonical UTF-8 JSON, CRC-32, length-prefixed log records |
| `log.py` | Force one record; drop a torn tail before the next append |
| `snapshot.py` | `RL04` snapshot with a header CRC and a body CRC |
| `publish.py` | Temp file in the destination directory, file flush, `os.replace`, directory flush |
| `store.py` | Transactions. Default order forces the commit record, then publishes |
| `recover.py` | Redo past the snapshot LSN, then undo loser transactions with compensation records |
| `migrate.py` | Element states `absent`, `delete_only`, `write_only`, `public` across schema versions 1–9 |
| `faults.py` | In-memory Model Z cuts and Model P images |
| `trace.py` | `logging.getLogger("recovery_lab")` phases. Recovery does not read this file |
| `outcomes.py` | The six symptom classes |

On disk, beside the data directory:

- `ledger.log` — the only checksummed log
- `ledger.snap` — the published snapshot
- `audit.log` — diagnostics, written when a handler is attached
- `checkpoint.hint` — a side file the recovery function never opens

A logical update stores the full before-image and after-image of one account, plus the email-index keys that image adds or removes. Redo and undo apply that image. They do not merge fields from two commits.

## Run

From the repository root:

```text
python -m unittest discover -s projects/python-maintenance-regression-lab-case-004/tests -v
```

From this directory:

```text
python run_lab.py metrics
python run_lab.py demo
```

`metrics` reprints the counts in `examples/measured_metrics.json`. `demo` commits `examples/accounts.json` in a temporary directory and prints the ledger fingerprint. Both stay offline.

## Design decisions

**The snapshot depends on the commit flush.** In the default protocol the trace records the snapshot write as depending on the log flush of that commit. A cut that drops the commit record also drops the snapshot, and recovery reinstalls the previous durable commit. `publish_before_log_flush` is a test-only switch that records the snapshot write with an empty dependency list, so the snapshot can remain when the commit write is cut. On this run that fault is `mixed_fields` at rank 1.

**A finished flush is kept even when the file is shorter than the block.** Model Z still cuts at every 512-byte and 4096-byte offset inside each write, including offset 0. An interrupted write contributes no bytes, because its flush depends on it. Rounding a finished short log down to zero would erase a commit the flush had already covered. Both block sizes on the default protocol are `durable_match` (6 cuts at 512 bytes and 6 cuts at 4096 bytes). Every write in the two-commit trace is shorter than 512 bytes (160–349 bytes), so each sweep places exactly one cut per write and the two sweeps produce the same six images. Because an interrupted write contributes nothing, the cut offset changes the rank order, not the image. The 12 Model Z cuts are six distinct crash states counted once per block size.

**Backfill and cleanup commit one row at a time, then a gate record.** A single open transaction would be undone after a crash, so filled rows would disappear. Each row update ends with its own commit. `backfill_complete` (schema 4) and `cleanup_complete` (schema 8) are the gate commits that make the next state legal. A second recovery, including one whose snapshot LSN was reset to force a full redo, appends no further backfill updates.

**Neighbors are one version from the stored header.** Versions 3 and 5 differ by two numbers, and so do 7 and 9. The header stays on the gate version (4 after backfill, 8 after cleanup), so each running process is one step from the bytes on disk. Behavior follows the process version: a version-3 writer still treats status as write-only while a version-5 reader treats it as public.

**A checksum-valid snapshot is applied even when its LSN is ahead of the log.** That is the bug the missing flush edge exists to expose. A snapshot whose CRC does not match is ignored and the log is replayed from an empty ledger. A missing snapshot is schema 1, so replaying a later schema keeps the header and rewinds `applied_lsn` instead of deleting the file.

**A short frame at the end of the log is a torn tail.** The next append truncates that tail first. A frame that fits and whose CRC does not match is a hole: recovery raises `StructuralCorruption` and writes nothing. That includes a full-length final frame with garbage inside it, so this reader refuses some images whose earlier commits could have been restored.

**An older process does not write past a record kind it cannot read.** Recovery leaves such a tail unapplied and reinstalls the last commit it understands. A commit, checkpoint, or undo that would append behind that tail raises `UnavailableSchemaGap` instead, because the appended record would put the unknown kind on the path to a durable commit and the next open would refuse the whole log.

**Email is unique per live account.** An upsert that reuses another account's email raises `ConstraintError`. Without that check the index key would move to the second account, and undoing that transaction would clear the key from the first.

**Directory fsync is attempted and recorded.** `element_state` is `supported` or `unsupported`. A failed directory flush does not roll the commit back; `os.replace` has already published the name. The lab does not register a boot-time delete. `plan_deferred_cleanup` only checks that file entries would be ordered before a directory entry.

## Measured run

`python run_lab.py metrics` classified **24** crash images.

- Model Z: **12** block cuts (6 at 512 bytes, 6 at 4096 bytes; six distinct images, see above), all `durable_match`.
- Publish-before-flush: first failing 512-byte cut is **rank 1** of 6 (limit 1), class `mixed_fields`, score 100.
- Undo: 10 crashes at the first compensation step and one crash at the second left **2** compensation records, and the ledger matched the pre-transaction state.
- Illegal public-insert / absent-delete fixture: **1** orphan index key.
- Staged neighbor sweep (pairs 1–2, 2–3, 3–5, 5–6, 6–7, 7–9): **0** orphan keys.

Those figures are `examples/measured_metrics.json`. They are counts from this program, not figures from the papers cited in `RESEARCH_APPLICATION.md`.

## What this demonstrates

- Same-directory publish: device check, temp name `.{dest}.tmp-{pid}-{lsn}`, file flush, `os.replace`, directory flush, in-process temp unlink.
- A failed `os.replace` leaves the previous snapshot bytes in place. The commit is already in the log, and the next recovery installs it.
- A cross-volume temp path raises `SameVolumeRequired` before the snapshot or the log grows.
- Redo skips records at or below the snapshot LSN. A second recovery applies zero update images.
- Compensation records are redo-only and chained. Repeating a crash before the force does not grow the log.
- Checkpoint records and `checkpoint.hint` are hints. Removing the hint, rewriting the checkpoint LSN to 1000000000, or dropping the checkpoint record leaves the same ledger hash, and the applied LSN stays on the commit.
- Six neighbor pairs keep a public index complete and never call a reader for `delete_only`, `write_only`, or `absent`. The three illegal fixtures each report an anomaly count above zero.
- Opening schema 5 against a checksum-valid header of 3 raises `UnavailableSchemaGap` and leaves the snapshot and log hashes unchanged.
- Five backfill crashes, one per row, finish with five status values. Recovery does not write those updates again.
- Each finished recovery emits one `phase=outcome` line carrying the class and `last_durable_lsn`. Replacing `audit.log` with random bytes leaves the recovered fingerprint unchanged.
- Audit lines are tagged with their data directory. Two ledgers open in one process do not write into each other's `audit.log`, and two stores on one directory share one handler, so each phase is written once.
- A reader keeps an unknown JSON field out of the account row. An unknown log kind after the last commit is left unapplied, and this process then refuses to append behind it. An unknown kind required to reach a commit refuses the open.

## Limitations

The crash images are an in-memory trace with explicit flush and dependency edges. The measured trace is two small commits, so the Model Z sweep is six distinct images, not a large search. They do not reproduce a particular kernel, delayed allocation, or a device cache. One checksummed log does not survive loss of that file. Directory fsync remains best-effort where the operating system refuses a directory handle. The migration matrix is this lab's contract for two live versions; it does not implement a distributed commit timestamp or a multi-key test-and-set. Unknown fields are ignored by copying the known account keys. There is no alias table and no numeric promotion.
