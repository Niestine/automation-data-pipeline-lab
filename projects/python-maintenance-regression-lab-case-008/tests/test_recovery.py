"""Every stable prefix of the three-transaction script matches the commit oracle."""

from __future__ import annotations

import json
import unittest

from helpers import ROOT, capture_logger

from prefixlab.audit import recovery_audit
from prefixlab.recover import restart
from prefixlab.txn import Store, apply_script
from prefixlab.wal import compensation_counts


def _script():
    return json.loads((ROOT / "examples" / "recovery_script.json").read_text(encoding="utf-8"))


class RecoveryTests(unittest.TestCase):
    def test_live_savepoint_image_omits_the_rolled_back_write(self):
        store = apply_script(Store(), _script())
        page = store.page("pageC")
        self.assertIn(b"keep-first", page)
        self.assertIn(b"keep-third", page)
        self.assertNotIn(b"drop-middle", page)
        self.assertEqual(store.page("pageB"), b"B-uncommitted")

    def test_every_prefix_restores_committed_images_only(self):
        report = recovery_audit(_script())
        self.assertEqual(report["mismatches"], 0)
        self.assertEqual(report["double_restart_extra"], 0)
        self.assertLessEqual(report["max_compensations"], 1)
        self.assertGreaterEqual(report["prefixes"], 10)
        self.assertEqual(report["stable_records"], report["logical_records"])

    def test_cut_before_a_commit_drops_that_transaction(self):
        store = apply_script(Store(), _script())
        commit = next(record for record in store.log.records if record.txn == "A" and record.kind == "commit")
        prefix = [record for record in store.log.records if record.lsn < commit.lsn]
        recovered = restart(dict(store.pages.initial), prefix)
        self.assertEqual(recovered.page("pageA"), b"base-A")
        self.assertEqual(recovered.page("pageB"), b"base-B")
        self.assertEqual(recovered.page("pageC"), b"base-C")

    def test_recovery_log_line_has_the_record_fields(self):
        store = apply_script(Store(), _script())
        _log, handler, lines = capture_logger()
        try:
            restart(dict(store.pages.initial), store.log.stable_records())
        finally:
            _log.removeHandler(handler)
        clr = next(r for r in store.log.records if r.kind == "clr" and r.compensates_lsn)
        self.assertIn(
            f"lsn={clr.lsn} txn=C kind=clr page=pageC undo_nxt_lsn={clr.undo_nxt_lsn}", lines
        )
        # Restart appends a compensation for loser B and logs it too.
        self.assertTrue(any("txn=B kind=clr page=pageB" in line for line in lines))
        self.assertTrue(any("txn=B kind=abort" in line for line in lines))
        counts = compensation_counts(store.log.records)
        self.assertTrue(counts)
        self.assertTrue(all(count == 1 for count in counts.values()))


if __name__ == "__main__":
    unittest.main()
