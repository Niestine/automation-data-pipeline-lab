"""Model P images: garbage sector, size-before-data, and non-atomic rename residues."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from recovery_lab.format import LOG_NAME
from recovery_lab.metrics import model_p_images, rename_images
from recovery_lab.outcomes import DURABLE_MATCH
from recovery_lab.recover import recover
from recovery_lab.store import LedgerStore


class ModelPTests(unittest.TestCase):
    def test_model_p_garbage_is_structural(self) -> None:
        rows = model_p_images()
        garbage = rows[0]
        self.assertEqual(garbage["outcome"], "structural_corruption")
        self.assertTrue(garbage["unchanged"])
        self.assertGreaterEqual(garbage["last_durable_lsn"], 0)

    def test_size_before_data_rebuilt_from_log(self) -> None:
        rows = model_p_images()
        unflushed = rows[1]
        flushed = rows[2]
        self.assertEqual(unflushed["outcome"], DURABLE_MATCH)
        self.assertEqual(unflushed["balance"], 10)
        self.assertEqual(flushed["outcome"], DURABLE_MATCH)
        self.assertEqual(flushed["balance"], 25)
        self.assertNotEqual(unflushed["balance"], 0)
        self.assertNotEqual(flushed["balance"], 0)

    def test_rename_residues_follow_the_log(self) -> None:
        rows = rename_images()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["outcome"], DURABLE_MATCH)
            self.assertEqual(row["balance"], 25)

    def test_torn_tail_is_not_a_hole(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                store.upsert(1, 25, "a@example.com")
                store.commit()
            log_path = root / LOG_NAME
            blob = log_path.read_bytes()
            log_path.write_bytes(blob[:-1])
            result = recover(root)
        self.assertEqual(result.outcome, DURABLE_MATCH)
        self.assertEqual(result.ledger.accounts[1]["balance"], 10)
        self.assertNotIn(2, result.ledger.accounts)
