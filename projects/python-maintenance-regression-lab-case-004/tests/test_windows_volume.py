"""Same-directory replace, cross-volume refusal, and a failed replace."""

from __future__ import annotations

import unittest
from pathlib import Path

import helpers  # noqa: F401

from recovery_lab.errors import SameVolumeRequired
from recovery_lab.format import LOG_NAME, SNAP_NAME
from recovery_lab.publish import plan_deferred_cleanup, files_before_directory
from recovery_lab.publish import CleanupEntry
from recovery_lab.recover import recover
from recovery_lab.snapshot import file_hash
from recovery_lab.store import LedgerStore


class _Device:
    def __init__(self, dev: int) -> None:
        self.st_dev = dev


def _split_stat(path: Path) -> _Device:
    if ".tmp-" in Path(path).name:
        return _Device(2)
    return _Device(1)


class WindowsVolumeTests(unittest.TestCase):
    def test_same_volume(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                before_snap = (root / SNAP_NAME).read_bytes()
                before_log = (root / LOG_NAME).read_bytes()
                store.stat_fn = _split_stat
                store.upsert(1, 25, "a@example.com")
                with self.assertRaises(SameVolumeRequired):
                    store.commit()
                self.assertEqual((root / SNAP_NAME).read_bytes(), before_snap)
                self.assertEqual((root / LOG_NAME).read_bytes(), before_log)
                self.assertEqual(list(root.glob(".ledger.snap.tmp-*")), [])

    def test_replace_oserror(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                before = file_hash(root / SNAP_NAME)

                def fail_replace(_src: Path, _dst: Path) -> None:
                    raise OSError("replace failed")

                store.replace_fn = fail_replace
                store.upsert(1, 25, "b@example.com")
                with self.assertRaises(OSError):
                    store.commit()
                self.assertEqual(file_hash(root / SNAP_NAME), before)
            result = recover(root)
        self.assertEqual(result.outcome, "durable_match")
        self.assertEqual(result.ledger.accounts[1]["balance"], 25)
        self.assertEqual(result.ledger.accounts[1]["email"], "b@example.com")
        self.assertEqual(result.last_durable_lsn, result.applied_lsn)

    def test_deferred_cleanup_orders_files_first(self) -> None:
        entries = plan_deferred_cleanup("snapdir", ["a.tmp", "b.tmp"])
        self.assertTrue(files_before_directory(entries))
        self.assertEqual([entry.kind for entry in entries], ["file", "file", "dir"])
        self.assertLess(
            min(index for index, entry in enumerate(entries) if entry.kind == "file"),
            max(index for index, entry in enumerate(entries) if entry.kind == "dir"),
        )
        reversed_entries = [CleanupEntry("dir", "snapdir"), CleanupEntry("file", "a.tmp")]
        self.assertFalse(files_before_directory(reversed_entries))

    def test_publish_unlinks_temp_and_records_directory_flush(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                self.assertEqual(list(root.glob(".ledger.snap.tmp-*")), [])
                text = (root / "audit.log").read_text(encoding="utf-8")
        self.assertIn("phase=dir_flush", text)
        self.assertTrue("element_state=supported" in text or "element_state=unsupported" in text)
