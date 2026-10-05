"""A checkpoint record is a hint. Recovery does not start from its LSN."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from recovery_lab.format import HINT_NAME
from recovery_lab.metrics import copy_directory, drop_checkpoint_records, rewrite_checkpoint_lsn
from recovery_lab.recover import recover
from recovery_lab.store import LedgerStore


class CheckpointHintTests(unittest.TestCase):
    def test_checkpoint_not_truth(self) -> None:
        with helpers.workspace() as built:
            with LedgerStore(built) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                store.upsert(1, 25, "b@example.com")
                commit_lsn = store.commit()
                store.checkpoint()
            self.assertTrue((built / HINT_NAME).exists())

            with helpers.workspace() as present:
                copy_directory(built, present / "case")
                root = present / "case"
                first = recover(root)

            with helpers.workspace() as removed:
                copy_directory(built, removed / "case")
                root = removed / "case"
                (root / HINT_NAME).unlink()
                self.assertFalse((root / HINT_NAME).exists())
                second = recover(root)

            with helpers.workspace() as rewritten:
                copy_directory(built, rewritten / "case")
                root = rewritten / "case"
                rewrite_checkpoint_lsn(root, 10**9)
                third = recover(root)

            with helpers.workspace() as dropped:
                copy_directory(built, dropped / "case")
                root = dropped / "case"
                drop_checkpoint_records(root)
                fourth = recover(root)

        fingerprints = [first.fingerprint, second.fingerprint, third.fingerprint, fourth.fingerprint]
        self.assertEqual(len(set(fingerprints)), 1)
        for result in (first, second, third, fourth):
            self.assertEqual(result.ledger.accounts[1]["balance"], 25)
            self.assertEqual(result.ledger.accounts[1]["email"], "b@example.com")
            self.assertEqual(result.applied_lsn, commit_lsn)
            self.assertNotEqual(result.applied_lsn, 10**9)
            self.assertEqual(result.outcome, "durable_match")
