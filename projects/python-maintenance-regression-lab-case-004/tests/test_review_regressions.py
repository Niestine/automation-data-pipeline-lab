"""Regressions for defects found in review: audit routing, future log tails, and row constraints."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from recovery_lab.errors import ConstraintError, UnavailableSchemaGap
from recovery_lab.format import LOG_NAME, parse_log
from recovery_lab.metrics import _frames, patch_kind
from recovery_lab.outcomes import DURABLE_MATCH
from recovery_lab.recover import directory_hashes, recover
from recovery_lab.store import LedgerStore


def _append_future_record(root) -> None:
    """Append a copy of the last frame with an unknown kind code."""
    blob = (root / LOG_NAME).read_bytes()
    start, end = _frames(blob)[-1]
    doubled = patch_kind(blob[start:end] + blob[start:end], 1, 9)
    (root / LOG_NAME).write_bytes(blob + doubled[end - start :])


class AuditRoutingTests(unittest.TestCase):
    def test_audit_lines_stay_in_their_own_directory(self) -> None:
        with helpers.workspace() as left, helpers.workspace() as right:
            with LedgerStore(left), LedgerStore(right) as writer:
                writer.upsert(1, 10, "a@example.com")
                writer.commit()
            left_audit = left / "audit.log"
            self.assertFalse(left_audit.exists() and left_audit.read_text(encoding="utf-8").strip())
            text = (right / "audit.log").read_text(encoding="utf-8")
        self.assertEqual(text.count("phase=log_flush"), 1)
        self.assertEqual(text.count("phase=replace"), 1)

    def test_two_stores_on_one_directory_write_each_line_once(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root), LedgerStore(root) as writer:
                writer.upsert(1, 10, "a@example.com")
                writer.commit()
            with LedgerStore(root) as again:
                again.upsert(1, 25, "a@example.com")
                again.commit()
            text = (root / "audit.log").read_text(encoding="utf-8")
        self.assertEqual(text.count("phase=log_flush"), 2)
        self.assertEqual(text.count("phase=dir_flush"), 2)


class FutureTailTests(unittest.TestCase):
    def test_older_writer_does_not_append_past_a_future_record(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
            _append_future_record(root)
            self.assertEqual(recover(root).outcome, DURABLE_MATCH)
            before = directory_hashes(root)
            with LedgerStore(root) as store:
                store.upsert(1, 30, "a@example.com")
                with self.assertRaises(UnavailableSchemaGap):
                    store.commit()
                with self.assertRaises(UnavailableSchemaGap):
                    store.checkpoint()
            self.assertEqual(directory_hashes(root), before)
            result = recover(root)
        self.assertEqual(result.outcome, DURABLE_MATCH)
        self.assertEqual(result.ledger.accounts[1]["balance"], 10)

    def test_undo_behind_a_future_record_is_refused_without_writes(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                store.upsert(1, 40, "a@example.com")
                store.durabilize_open_transaction()
            _append_future_record(root)
            before = directory_hashes(root)
            with self.assertRaises(UnavailableSchemaGap):
                recover(root)
            self.assertEqual(directory_hashes(root), before)
            kinds = [record.kind for record in parse_log((root / LOG_NAME).read_bytes())]
        self.assertNotIn("compensation", kinds)


class RowConstraintTests(unittest.TestCase):
    def test_duplicate_email_does_not_take_another_accounts_key(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                with self.assertRaises(ConstraintError):
                    store.upsert(2, 5, "a@example.com")
                self.assertIsNone(store.commit())
                self.assertEqual(store.read_email("a@example.com"), 1)
                store.upsert(1, 11, "a@example.com")
                store.commit()
            result = recover(root)
        self.assertEqual(result.ledger.email_index, {"a@example.com": 1})
        self.assertNotIn(2, result.ledger.accounts)

    def test_failed_delete_leaves_no_open_transaction(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                with self.assertRaises(ConstraintError):
                    store.delete(7)
                self.assertFalse(store._tx_open)
                with LedgerStore(root) as other:
                    other.upsert(1, 10, "a@example.com")
                    other.commit()
                self.assertEqual(store.read_email("a@example.com"), 1)
                store.delete(1)
                store.commit()
            result = recover(root)
        self.assertEqual(result.ledger.accounts, {})
        self.assertEqual(result.ledger.email_index, {})


if __name__ == "__main__":
    unittest.main()
