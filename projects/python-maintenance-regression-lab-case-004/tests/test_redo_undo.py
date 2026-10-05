"""Redo skips a snapshot LSN. Undo of one transaction stays bounded across crashes."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from recovery_lab.format import LOG_NAME, SNAP_NAME, parse_log
from recovery_lab.metrics import undo_compensation_count
from recovery_lab.recover import recover
from recovery_lab.snapshot import fingerprint
from recovery_lab.store import LedgerStore


class RedoUndoTests(unittest.TestCase):
    def test_redo_skips_applied_lsn(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                store.upsert(1, 25, "a@example.com")
                store.commit()
            (root / SNAP_NAME).unlink()
            first = recover(root)
            second = recover(root)
        self.assertGreater(first.apply_count, 0)
        self.assertEqual(second.apply_count, 0)
        self.assertEqual(second.applied_lsn, first.applied_lsn)
        self.assertEqual(second.fingerprint, first.fingerprint)
        self.assertEqual(second.ledger.accounts[1]["balance"], 25)

    def test_undo_bound(self) -> None:
        count = undo_compensation_count()
        self.assertEqual(count, 2)

    def test_undo_log_contains_two_compensations(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                before = fingerprint(store.ledger)
                store.upsert(1, 40, "a@example.com")
                store.upsert(3, 7, "c@example.com")
                store.durabilize_open_transaction()
            from recovery_lab.errors import SimulatedCrash
            from recovery_lab.recover import UndoInjector

            injector = UndoInjector()
            injector.crash_budget[1] = 10
            size = (root / LOG_NAME).stat().st_size
            for _ in range(10):
                with self.assertRaises(SimulatedCrash):
                    recover(root, injector=injector)
                self.assertEqual((root / LOG_NAME).stat().st_size, size)
            self.assertEqual(
                sum(1 for record in parse_log((root / LOG_NAME).read_bytes()) if record.kind == "compensation"),
                0,
            )
            injector.crash_budget[2] = 1
            with self.assertRaises(SimulatedCrash):
                recover(root, injector=injector)
            result = recover(root, injector=injector)
        self.assertEqual(result.compensation_count, 2)
        self.assertEqual(result.fingerprint, before)
        self.assertEqual(result.ledger.accounts[1]["balance"], 10)
        self.assertNotIn(3, result.ledger.accounts)
