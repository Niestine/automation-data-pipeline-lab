"""Adjacent schema versions, illegal one-step fixtures, and crash-resume backfill."""

from __future__ import annotations

import json
import struct
import unittest

import helpers  # noqa: F401

from recovery_lab.errors import MigrationError, UnavailableSchemaGap
from recovery_lab.format import LOG_NAME, MAGIC, SNAP_NAME, canonical_json, crc32, parse_log
from recovery_lab.snapshot import encode_snapshot, read_snapshot
from recovery_lab.metrics import neighbor_report, rewrite_schema_version
from recovery_lab.migrate import Migrator, assert_transition, integrity
from recovery_lab.recover import directory_hashes, recover
from recovery_lab.snapshot import decode_snapshot
from recovery_lab.store import LedgerStore


class SchemaNeighborTests(unittest.TestCase):
    def test_schema_neighbors(self) -> None:
        report = neighbor_report()
        self.assertEqual(len(report["pairs"]), 6)
        self.assertTrue(report["blocked_before_backfill"])
        self.assertTrue(report["blocked_before_cleanup"])
        self.assertEqual(report["orphan_total"], 0)
        for row in report["pairs"]:
            self.assertEqual(row["orphans"], 0, row)
            self.assertEqual(row["missing"], 0, row)
            self.assertEqual(row["illegal_reads"], 0, row)
            self.assertEqual(row["reader_violations"], 0, row)
            if row["newer"] >= 5:
                self.assertEqual(row["status_missing"], 0, row)
                self.assertEqual(row["status_hit"], "active", row)
            if row["older"] < 6 and row["newer"] <= 6:
                self.assertEqual(row["email_hit"], 10, row)

    def test_one_step_add_index_orphans(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as writer:
                writer.upsert(1, 10, "a@example.com")
                writer.commit()
            with LedgerStore(root, email_state="absent") as deleter:
                deleter.delete(1)
                deleter.commit()
            with LedgerStore(root) as reader:
                reader._load_idle()
                report = integrity(reader.ledger, email_public=True, status_public=False)
        self.assertGreater(report.orphan_index_keys, 0)
        self.assertGreater(report.anomaly_count, 0)

    def test_one_step_required_status(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                report = integrity(store.ledger, email_public=True, status_public=True)
        self.assertGreater(report.rows_missing_status, 0)
        self.assertGreater(report.anomaly_count, 0)

    def test_drop_index_while_read(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as reader:
                reader.upsert(1, 10, "a@example.com")
                reader.commit()
            with LedgerStore(root, email_state="absent") as writer:
                def blocked(*_args, **_kwargs):
                    raise AssertionError("reader used")

                writer.read_email = blocked  # type: ignore[method-assign]
                writer.read_status = blocked  # type: ignore[method-assign]
                writer.upsert(1, 10, "b@example.com")
                writer.commit()
            with LedgerStore(root) as reader:
                self.assertEqual(reader.read_email("a@example.com"), 1)
                reader._load_idle()
                report = integrity(reader.ledger, email_public=True, status_public=False)
        self.assertGreater(report.anomaly_count, 0)
        self.assertGreater(report.orphan_index_keys, 0)

    def test_gap(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
                Migrator(store).advance_to(5)
            rewrite_schema_version(root, 3)
            before = directory_hashes(root)
            with self.assertRaises(UnavailableSchemaGap) as caught:
                recover(root, code_version=5)
            self.assertEqual(directory_hashes(root), before)
            self.assertEqual(caught.exception.code_version, 5)
            self.assertEqual(caught.exception.stored_version, 3)
            self.assertGreaterEqual(caught.exception.last_durable_lsn, 0)
            with self.assertRaises(UnavailableSchemaGap):
                LedgerStore(root, schema_version=5)
            self.assertEqual(directory_hashes(root), before)

    def test_backfill_crash(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                for account_id in range(5):
                    store.upsert(account_id, account_id * 10, f"u{account_id}@example.com")
                    store.commit()
                Migrator(store).advance_to(3)
                migrator = Migrator(store)
                from recovery_lab.errors import SimulatedCrash

                for _ in range(5):
                    with self.assertRaises(SimulatedCrash):
                        migrator.backfill(crash_after=0)
                store._load_idle()
                filled = sum(1 for account in store.ledger.accounts.values() if account.get("status"))
            self.assertEqual(filled, 5)
            before = sum(1 for record in parse_log((root / LOG_NAME).read_bytes()) if record.kind == "update")
            recover(root, code_version=3)
            self.assertEqual(
                sum(1 for record in parse_log((root / LOG_NAME).read_bytes()) if record.kind == "update"),
                before,
            )
            snapped, _blob = read_snapshot(root)
            self.assertIsNotNone(snapped)
            assert snapped is not None
            snapped.applied_lsn = 0
            (root / SNAP_NAME).write_bytes(encode_snapshot(snapped))
            result = recover(root, code_version=3)
            self.assertEqual(
                sum(1 for record in parse_log((root / LOG_NAME).read_bytes()) if record.kind == "update"),
                before,
            )
            self.assertEqual(sum(1 for account in result.ledger.accounts.values() if account.get("status")), 5)

    def test_migrator_rejects_skips_and_early_gates(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 1, "a@example.com")
                store.commit()
                with self.assertRaises(MigrationError):
                    store.transition(3)
                Migrator(store).advance_to(3)
                with self.assertRaises(MigrationError):
                    store.transition(4)
                Migrator(store).advance_to(7)
                with self.assertRaises(MigrationError):
                    store.transition(8)

        class Ledger:
            schema_version = 4
            accounts: dict = {}
            backfill_complete = False
            cleanup_complete = False
            email_index: dict = {}

        with self.assertRaises(MigrationError):
            assert_transition(Ledger(), 5)

    def test_reader_ignores_unknown_json_key(self) -> None:
        with helpers.workspace() as root:
            with LedgerStore(root) as store:
                store.upsert(1, 10, "a@example.com")
                store.commit()
            blob = (root / SNAP_NAME).read_bytes()
            patched = _insert_region(blob)
            self.assertIsNotNone(decode_snapshot(patched))
            decoded = decode_snapshot(patched)
            assert decoded is not None
            self.assertEqual(decoded.accounts[1]["balance"], 10)
            self.assertNotIn("region", decoded.accounts[1])
            (root / SNAP_NAME).write_bytes(patched)
            result = recover(root)
        self.assertEqual(result.ledger.accounts[1]["balance"], 10)
        self.assertEqual(result.outcome, "durable_match")


def _insert_region(blob: bytes) -> bytes:
    offset = len(MAGIC)
    schema_version, applied_lsn = struct.unpack_from("<IQ", blob, offset)
    offset += 12
    (states_len,) = struct.unpack_from("<I", blob, offset)
    offset += 4
    states = blob[offset : offset + states_len]
    offset += states_len + 4
    (body_len,) = struct.unpack_from("<I", blob, offset)
    offset += 4
    body = blob[offset : offset + body_len]
    payload = json.loads(body.decode("utf-8"))
    payload["accounts"][0]["region"] = "lab"
    new_body = canonical_json(payload)
    out = bytearray()
    out += MAGIC
    out += struct.pack("<IQ", schema_version, applied_lsn)
    out += struct.pack("<I", len(states))
    out += states
    out += struct.pack("<I", crc32(states))
    out += struct.pack("<I", len(new_body))
    out += new_body
    out += struct.pack("<I", crc32(new_body))
    return bytes(out)
