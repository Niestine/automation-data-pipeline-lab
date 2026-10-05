"""One outcome class per finished recovery, and the audit file is not an input."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

import helpers  # noqa: F401

from recovery_lab.errors import ConstraintError, IllegalRead, StructuralCorruption, UnavailableSchemaGap
from recovery_lab.format import LOG_NAME
from recovery_lab.metrics import patch_kind
from recovery_lab.outcomes import DURABLE_MATCH, STRUCTURAL_CORRUPTION, UNAVAILABLE_GAP
from recovery_lab.recover import directory_hashes, recover
from recovery_lab.store import LedgerStore
from recovery_lab.trace import attach, detach


class DiagnosticTests(unittest.TestCase):
    def test_outcome_records_one_class_and_lsn(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
            text = (root / "audit.log").read_text(encoding="utf-8")
            for phase in ("log_append", "log_flush", "snapshot_write", "snapshot_flush", "replace", "dir_flush"):
                self.assertIn(f"phase={phase}", text)
            handler = attach(root)
            try:
                result = recover(root)
            finally:
                detach(handler)
            outcome_lines = [
                line for line in (root / "audit.log").read_text(encoding="utf-8").splitlines() if "phase=outcome" in line
            ]
        self.assertEqual(len(outcome_lines), 1)
        self.assertIn(f"outcome={DURABLE_MATCH}", outcome_lines[0])
        self.assertIn(f"lsn={result.last_durable_lsn}", outcome_lines[0])
        self.assertEqual(result.outcome, DURABLE_MATCH)

    def test_structural_and_gap_leave_bytes_and_name_a_class(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                store.upsert(1, 25, "a@example.com")
                store.commit()
            blob = bytearray((root / LOG_NAME).read_bytes())
            blob[40] ^= 0x5A
            (root / LOG_NAME).write_bytes(bytes(blob))
            before = directory_hashes(root)
            handler = attach(root)
            try:
                with self.assertRaises(StructuralCorruption) as caught:
                    recover(root)
            finally:
                detach(handler)
            self.assertEqual(directory_hashes(root), before)
            lines = [
                line
                for line in (root / "audit.log").read_text(encoding="utf-8").splitlines()
                if "phase=outcome" in line and f"outcome={STRUCTURAL_CORRUPTION}" in line
            ]
            self.assertEqual(len(lines), 1)
            self.assertIn(f"lsn={caught.exception.last_durable_lsn}", lines[0])

        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
            patched = patch_kind((root / LOG_NAME).read_bytes(), 0, 9)
            (root / LOG_NAME).write_bytes(patched)
            before = directory_hashes(root)
            handler = attach(root)
            try:
                with self.assertRaises(UnavailableSchemaGap) as gap:
                    recover(root)
            finally:
                detach(handler)
            self.assertEqual(directory_hashes(root), before)
            lines = [
                line
                for line in (root / "audit.log").read_text(encoding="utf-8").splitlines()
                if "phase=outcome" in line and f"outcome={UNAVAILABLE_GAP}" in line
            ]
            self.assertEqual(len(lines), 1)
            self.assertIn(f"lsn={gap.exception.last_durable_lsn}", lines[0])

    def test_future_unknown_tail_restores_the_commit(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
            clean = recover(root)
            blob = (root / LOG_NAME).read_bytes()
            from recovery_lab.metrics import _frames

            start, end = _frames(blob)[-1]
            extra = patch_kind(blob[start:end] + blob[start:end], 1, 9)
            # ``patch_kind`` above sees two frames because the slice was doubled.
            (root / LOG_NAME).write_bytes(blob + extra[end - start :])
            result = recover(root)
        self.assertEqual(result.outcome, DURABLE_MATCH)
        self.assertEqual(result.fingerprint, clean.fingerprint)
        self.assertEqual(result.ledger.accounts[1]["balance"], 10)

    def test_audit_bytes_do_not_change_the_ledger_hash(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
            first = recover(root)
            (root / "audit.log").write_bytes(os.urandom(64))
            second = recover(root)
        self.assertEqual(second.fingerprint, first.fingerprint)
        self.assertEqual(second.outcome, DURABLE_MATCH)
        self.assertEqual(second.ledger.accounts[1]["balance"], 10)

    def test_constraints_and_illegal_read(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                with self.assertRaises(ConstraintError):
                    store.upsert(1, True, "a@example.com")  # type: ignore[arg-type]
                with self.assertRaises(ConstraintError):
                    store.upsert(1, 10, "not-an-email")
                with self.assertRaises(ConstraintError):
                    store.upsert(1, 10, "a @example.com")
                store.upsert(1, 10, "a@example.com")
                store.commit()
            with LedgerStore(root, email_state="delete_only") as store:
                with self.assertRaises(IllegalRead):
                    store.read_email("a@example.com")
                self.assertEqual(store.illegal_reads, 1)
            with LedgerStore(root, status_state="write_only") as store:
                with self.assertRaises(IllegalRead):
                    store.read_status(1)
                self.assertEqual(store.illegal_reads, 1)

    def test_metrics_command(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(helpers.ROOT / "run_lab.py"), "metrics"],
            cwd=str(helpers.ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("model_z_cut_count", payload)
        self.assertTrue(payload["model_z_all_durable_match"])
        self.assertGreater(payload["model_z_cut_count"], 0)
        self.assertLessEqual(payload["first_failure_rank"], payload["first_failure_limit"])
        self.assertIn(payload["first_failure_class"], ("data_loss", "mixed_fields"))
        self.assertEqual(payload["compensation_count"], 2)
        self.assertGreater(payload["illegal_orphan_count"], 0)
        self.assertEqual(payload["staged_orphan_count"], 0)
        self.assertEqual(payload["model_p_unflushed_balance"], 10)
        self.assertEqual(payload["model_p_flushed_balance"], 25)
        self.assertTrue(payload["model_p_garbage_structural"])
        self.assertTrue(payload["rename_all_durable_match"])
        self.assertEqual(
            payload["permutation_count"],
            payload["model_z_cut_count"]
            + payload["publish_before_flush_cut_count"]
            + payload["rename_permutation_count"]
            + payload["model_p_image_count"],
        )
