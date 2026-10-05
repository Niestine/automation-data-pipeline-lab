# Research application

Five research sources shape this lab: the Pillai, Zheng, Mohan, and Rae papers and the MoveFileExW reference. Three API reference pages (POSIX `fsync`, POSIX `rename`, Python `os.replace`) document the calls the publish path uses; they are cited for API behavior, not as research findings. Each technique below is implemented under `src/recovery_lab` and locked by `tests/`. The accounts are synthetic. Counts in `README.md` and `examples/measured_metrics.json` come from `python run_lab.py metrics` on this program: 24 crash images, 12 Model Z cuts, a publish-before-flush failure at rank 1, 2 compensation records after the undo crashes, 1 orphan key on the illegal index fixture, and 0 orphan keys on the staged neighbor sweep.

## Flush the commit, then publish the snapshot

Sources:

- Thanumalayan Sankaranarayana Pillai, Vijay Chidambaram, Ramnatthan Alagappan, Samer Al-Kiswany, Andrea C. Arpaci-Dusseau, and Remzi H. Arpaci-Dusseau, "All File Systems Are Not Created Equal: On the Complexity of Crafting Crash-Consistent Applications", OSDI 2014. https://www.usenix.org/conference/osdi14/technical-sessions/presentation/pillai
- The Open Group, "fsync", IEEE Std 1003.1-2017. https://pubs.opengroup.org/onlinepubs/9699919799/functions/fsync.html
- The Open Group, "rename", IEEE Std 1003.1-2017. https://pubs.opengroup.org/onlinepubs/9699919799/functions/rename.html
- Python Software Foundation, "os.replace". https://docs.python.org/3/library/os.html#os.replace
- Microsoft, "MoveFileExW". https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw

`publish_file` creates the temp file in the destination directory, compares device ids, flushes the temp file, and calls `os.replace`. A different device id raises `SameVolumeRequired` before the destination is opened (`tests/test_windows_volume.py`). A replacement that raises `OSError` leaves the previous snapshot hash unchanged; the commit record is already in the log, and the next `recover` installs that commit.

The trace records a dependency from the snapshot write to the log flush of the same commit. Model Z cuts of that protocol are all `durable_match` at both block sizes (`tests/test_publish_edges.py`). Every write in the measured trace is under 512 bytes, so both sweeps cut each of the six writes once and yield the same six images. `publish_before_log_flush` records the snapshot write with an empty dependency so the published image can outlive a cut of the commit record. On the measured run the first failing 512-byte cut is rank 1, class `mixed_fields`, score 100, inside the limit `ceil(0.05 * N)` where N is 6.

`directory_flush` opens the parent and calls `os.fsync`. Failure is stored as `element_state=unsupported` and the commit stands. The lab does not schedule `MOVEFILE_DELAY_UNTIL_REBOOT`. `plan_deferred_cleanup` only builds the order a regression checks: every file entry before the directory entry, matching the rule that a delayed directory removal waits until the directory is empty.

POSIX `rename` keeps the destination name visible throughout the call. The three residues in `tests/test_model_p_atomicity.py` (both names, neither name, temp only) recover balance 25 from the flushed log. `os.replace` is the publish call so an existing destination is replaced on Windows and on POSIX.

## Two crash images, scored against committed states

Sources:

- Mai Zheng, Joseph Tucek, Dachuan Huang, Feng Qin, Mark Lillibridge, Elizabeth S. Yang, Bill W. Zhao, and Shashank Singh, "Torturing Databases for Fun and Profit", OSDI 2014. https://www.usenix.org/conference/osdi14/technical-sessions/presentation/zheng_mai
- Pillai et al., OSDI 2014, same paper as above.

Model Z (`faults.materialize`) follows the clean power-fault rule used here: a completed flush is kept in full, an interrupted write adds no bytes, and a later operation is dropped when it depends on a dropped one. Cut points are the 512-byte offsets and, separately, the 4096-byte offsets inside each write. A finished flush shorter than the block is kept. Rounding that flush down to an empty file would hide a durable commit. `tests/test_model_z_oracle.py` cuts inside the second commit record and recovers balance 10, class `durable_match`.

Model P is separate (`tests/test_model_p_atomicity.py`). The last 512 bytes of a flushed log are filled with `0x58` inside a framed record, which raises `StructuralCorruption` and leaves the file hashes unchanged. Extending an older snapshot with `0x00` up to the newer snapshot's length makes that snapshot fail its framing check. With only the first commit flushed, recovery rebuilds balance 10. With both commits flushed, recovery rebuilds balance 25. The zero-filled bytes are not installed as a balance.

Ranking sorts a cut higher when it intersects a commit record (score 100), then when it sits in a write after a log flush and before the next replace (80), then an account or index payload (40); everything else scores 0. Cuts are taken only inside writes, so the replace itself is not a ranked cut point. Equal scores break by byte offset. The rank is a search order. It is not a claim that every possible crash was enumerated. Symptom classes stay distinct: `durable_match`, `data_loss`, `mixed_fields`, `structural_corruption`, `refused_but_recoverable`, and `unavailable_gap`. Success is equality with the log-only redo-then-undo oracle.

The papers' vulnerability totals and speedups are not results of this lab.

## Repeat history, then compensate the losers

Source: C. Mohan, Don Haderle, Bruce Lindsay, Hamid Pirahesh, and Peter Schwarz, "ARIES: A Transaction Recovery Method Supporting Fine-Granularity Locking and Partial Rollbacks Using Write-Ahead Logging", ACM TODS 1992. https://web.stanford.edu/class/cs345d-01/rl/aries.pdf

The log is forced through the commit LSN before the snapshot is published. The snapshot's `applied_lsn` plays the role of a page LSN: redo skips a record at or below that value. `tests/test_redo_undo.py` deletes the snapshot, recovers, and recovers again. The second call's apply count is 0 and the fingerprint matches the first.

Loser transactions are those with an update and no commit, whose updates are not all compensated. Undo appends a compensation record whose after-image is the update's before-image, whose `undone_lsn` is the update, and whose `undo_next_lsn` is that update's `prev_lsn`. The compensation is forced before the next one. A crash raised before that force does not append. Ten crashes at undo step 1 leave the log the same size. One crash at step 2 leaves a single compensation. The clean recovery writes the second. The compensation count is 2, and the ledger matches the pre-transaction fingerprint.

Each update carries one account image plus the index keys that image sets or clears. Redo of a compensation applies that absolute image again, which is idempotent.

A checkpoint record stores a hint LSN and does not publish a snapshot. `recover` skips `kind=checkpoint` and never opens `checkpoint.hint`. `tests/test_checkpoint_hint.py` compares four ledgers: hint present, hint removed, checkpoint LSN rewritten to 1000000000, and the checkpoint record removed. One fingerprint, and the applied LSN stays on the user commit.

This lab has one checksummed log. Losing that file is not recoverable. The ARIES paper's buffer and lock managers are outside this process.

## One element state at a time

Source: Ian Rae, Eric Rollins, Jeff Shute, Sukhdeep Sodhi, and Radek Vingralek, "Online, Asynchronous Schema Change in F1", PVLDB 6(11), 2013. https://www.vldb.org/pvldb/vol6/p1045-rae.pdf

The element order is `absent`, `delete_only`, `write_only`, `public`. `delete_only` removes a key on delete and does not add one. `write_only` maintains the key and is not read. `public` is the only state a reader may use. Status becomes required only after a durable `backfill_complete` commit. The email index disappears only after a durable `cleanup_complete` commit. The version table and the tests are this lab's contract. They are not a quotation of the paper's section text, and the lab does not claim a distributed commit timestamp.

`tests/test_schema_neighbors.py` runs pairs (1, 2), (2, 3), (3, 5) after the backfill gate, (5, 6), (6, 7), and (7, 9) after the cleanup gate. Orphan keys are 0, illegal reads are 0, and a replaced reader method fails the test if a non-public element is read. Opening code version 5 against stored schema 3, and code version 9 against stored schema 7, raises `UnavailableSchemaGap` until the gate commit is durable. After the gate, the header is 4 or 8, so each neighbor is one version from the stored schema.

The illegal fixtures stay in the tree. A public insert followed by an absent-state delete leaves 1 orphan key. A ledger whose rows have no status, scored as if status were required, reports a missing status. A public reader still sees the old index key after an absent-state writer changes the email. Each anomaly count is greater than zero.

Backfill commits one row at a time. Five injected crashes fill five rows. Recovery appends no extra update per row, including when `applied_lsn` is rewritten to 0 so redo runs again. A direct jump, a required status before the gate, and a cleanup gate while index keys remain all raise `MigrationError`.

An unknown JSON key on an account is ignored by the snapshot decoder. An unknown log kind after the last commit is left unapplied and the outcome is `durable_match`. An unknown kind that sits on the path to a durable commit raises `UnavailableSchemaGap` and does not rewrite the files. Once a future tail is present, this process refuses to append a commit, checkpoint, or compensation behind it (`tests/test_review_regressions.py`), so it cannot turn a skippable tail into a required one. There is no alias map and no numeric promotion.

## The audit file is a view, not an input

`trace.attach` installs a handler on `recovery_lab` that appends one line and closes the file. Each line is tagged with its data directory, and a handler writes only lines for its own directory; a second `attach` on the same directory reuses the first handler. `recover` calls the logger and does not open `audit.log`. A finished recovery emits one `phase=outcome` line with the class and `last_durable_lsn` (`tests/test_diagnostics.py`). Replacing `audit.log` with random bytes leaves the recovered fingerprint unchanged. Phases used on the commit path are `log_append`, `log_flush`, `snapshot_write`, `snapshot_flush`, `replace`, and `dir_flush`. Redo, undo, compensate, and migrate are emitted when those steps run.

## Limits kept in view

The dependency edge is what makes the missing flush show up inside the first 5 percent of the ranked cuts. A pure time prefix of "publish, then flush" can place the high-score commit cuts before the snapshot exists, so those cuts still look durable. The empty dependency is the recording of that reorder.

Per-row commits are how backfill progress survives a crash. One uncommitted backfill transaction would be undone by the compensation path.

A checksum-valid snapshot whose LSN is not in the log is trusted on purpose. Discarding it would hide the bug the bad switch is there to show.

This write-up does not use Avro resolution, a SQLite rollback journal, or a claim about a specific filesystem's crash behavior.
