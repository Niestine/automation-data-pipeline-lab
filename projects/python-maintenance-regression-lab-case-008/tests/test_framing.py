"""Log framing, the force watermark, and page-LSN idempotence."""

from __future__ import annotations

import unittest

from helpers import ROOT

from prefixlab.pages import PageStore
from prefixlab.txn import Store
from prefixlab.wal import encode_records, stable_prefix


class FramingTests(unittest.TestCase):
    def test_unforced_tail_is_the_discarded_crash_image(self):
        store = Store()
        store.open_page("pageA", b"base-A")
        store.begin("A")
        store.update("A", "pageA", b"A-committed")
        self.assertEqual(store.log.stable_records(), [])
        dropped = store.log.crash_drop_tail()
        self.assertEqual([record.kind for record in dropped], ["begin", "update"])
        self.assertEqual(store.log.records, [])
        self.assertEqual(store.page("pageA"), b"A-committed")

    def test_force_before_commit_undoes_and_commit_keeps_the_image(self):
        store = Store()
        store.open_page("pageA", b"base-A")
        store.begin("A")
        updated = store.update("A", "pageA", b"A-committed")
        store.log.force(updated.lsn)
        from prefixlab.recover import restart

        before = restart({"pageA": b"base-A"}, store.log.stable_records())
        self.assertEqual(before.page("pageA"), b"base-A")
        store.commit("A")
        after = restart({"pageA": b"base-A"}, store.log.stable_records())
        self.assertEqual(after.page("pageA"), b"A-committed")

    def test_torn_tail_is_ignored(self):
        store = Store()
        store.open_page("pageA", b"base-A")
        store.begin("A")
        store.update("A", "pageA", b"A-committed")
        store.commit("A")
        raw = encode_records(store.log.stable_records())
        parsed = stable_prefix(raw[:-3])
        self.assertEqual(len(parsed), len(store.log.stable_records()) - 1)
        self.assertEqual(stable_prefix(raw), store.log.stable_records())
        self.assertEqual(stable_prefix(raw[:2]), [])

    def test_torn_commit_frame_rolls_the_transaction_back(self):
        from prefixlab.recover import restart

        store = Store()
        store.open_page("pageA", b"base-A")
        store.begin("A")
        store.update("A", "pageA", b"A-committed")
        store.commit("A")
        raw = store.log.encode_stable()
        whole = restart({"pageA": b"base-A"}, stable_prefix(raw))
        self.assertEqual(whole.page("pageA"), b"A-committed")
        torn = restart({"pageA": b"base-A"}, stable_prefix(raw[:-1]))
        self.assertEqual(torn.page("pageA"), b"base-A")
        self.assertEqual(torn.txns["A"].state, "aborted")

    def test_transaction_id_cannot_be_reused(self):
        store = Store()
        store.open_page("pageA", b"base-A")
        store.begin("A")
        store.commit("A")
        with self.assertRaises(ValueError):
            store.begin("A")

    def test_replay_of_an_applied_lsn_does_not_change_bytes(self):
        pages = PageStore()
        pages.open_page("pageA", b"base-A")
        store = Store()
        store.pages = pages
        store.begin("A")
        record = store.update("A", "pageA", b"A-committed")
        self.assertEqual(pages.get("pageA"), b"A-committed")
        self.assertFalse(pages.apply(record))
        self.assertEqual(pages.get("pageA"), b"A-committed")

    def test_bad_record_and_bad_script_are_refused(self):
        with self.assertRaises(ValueError):
            stable_prefix(b"\x00\x00\x00\x02{}")
        from prefixlab.txn import apply_script

        with self.assertRaises(ValueError):
            apply_script(Store(), {"pages": [], "steps": []})

    def test_example_script_loads(self):
        text = (ROOT / "examples" / "recovery_script.json").read_text(encoding="utf-8")
        self.assertIn("keep-first+keep-third", text)


if __name__ == "__main__":
    unittest.main()
