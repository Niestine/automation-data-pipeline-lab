"""Barrier checkpoint, split-write gap, and the single-scope lease."""

from __future__ import annotations

import threading
import unittest
from collections import Counter

import helpers
from plot_allotment.client import ExportClient
from plot_allotment.errors import CheckpointIOError, CheckpointMismatch, LeaseDenied
from plot_allotment.schema import param_fingerprint
from plot_allotment.worker import ExportWorker

PARENT = helpers.PARENT


def _seed(lab: helpers.Lab) -> None:
    for index, note in enumerate(("open", "open", "closed", "open", "open", "open", "open"), start=1):
        lab.add(helpers.lot(f"lot-{index:02d}", index, note=note))


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lab = helpers.Lab(seed=4)
        self.addCleanup(self.lab.close)
        _seed(self.lab)

    def test_split_gap_replays_one_page_and_checkpoint_error_is_visible(self) -> None:
        self.lab.store.fail_next_checkpoint = True
        with self.assertRaises(CheckpointIOError):
            self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="split")
        self.assertTrue(self.lab.journal.has("checkpoint_io", "surface_error"))
        self.assertEqual(self.lab.store.ledger_count(PARENT), 2)
        self.assertEqual(self.lab.store.checkpoint("north")["apply_ahead"], 1)
        report = self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="split")
        self.assertTrue(report.replayed)
        self.assertTrue(self.lab.journal.has("gap_detected", "replay_uncheckpointed_page"))
        ids = self.lab.store.ledger_ids(PARENT)
        self.assertEqual(ids, ["lot-01", "lot-02", "lot-04", "lot-05", "lot-06", "lot-07"])
        self.assertNotIn("lot-03", ids)
        counts = Counter(resource_id for _parent, resource_id in self.lab.store.executions)
        self.assertTrue(counts)
        self.assertTrue(all(value == 1 for value in counts.values()))
        self.assertEqual(self.lab.store.checkpoint("north")["apply_ahead"], 0)
        self.assertEqual(self.lab.store.checkpoint("north")["done"], 1)
        snapshot_id = self.lab.store.checkpoint("north")["snapshot_id"]
        self.assertEqual(self.lab.store.snapshot_ids(snapshot_id), ids)

    def test_checkpoint_then_resume_does_not_reexecute_earlier_rows(self) -> None:
        self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="atomic", max_pages=1)
        first = list(self.lab.store.executions)
        self.assertEqual(len(first), 2)
        self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="atomic")
        counts = Counter(resource_id for _parent, resource_id in self.lab.store.executions)
        self.assertEqual(len(counts), 6)
        self.assertTrue(all(value == 1 for value in counts.values()))
        self.assertEqual(self.lab.store.executions[:2], first)

    def test_second_worker_loses_the_lease(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.lab.worker.hold_after_lease = (entered, release)
        holder: list = []

        def run_holder() -> None:
            try:
                holder.append(
                    self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="atomic")
                )
            except Exception as exc:  # pragma: no cover - fail the assertion below
                holder.append(exc)

        thread = threading.Thread(target=run_holder)
        thread.start()
        self.assertTrue(entered.wait(5))
        other_client = ExportClient(
            self.lab.service,
            helpers.CALLER,
            sleeper=self.lab.sleeper,
            rng=self.lab.rng,
            journal=self.lab.journal,
        )
        other = ExportWorker(
            self.lab.service,
            self.lab.store,
            other_client,
            self.lab.journal,
            self.lab.clock,
            owner="worker-b",
        )
        before = self.lab.store.ledger_count(PARENT)
        with self.assertRaises(LeaseDenied):
            other.run("north", PARENT, "note=open", "sort_key", 2, mode="atomic")
        self.assertEqual(self.lab.store.ledger_count(PARENT), before)
        self.assertTrue(self.lab.journal.has("lease_denied", "exit_without_writes"))
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.lab.store.ledger_count(PARENT), 6)

    def test_split_gap_on_a_later_page_refetches_the_applied_token(self) -> None:
        self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="split", max_pages=1)
        self.lab.store.fail_next_checkpoint = True
        with self.assertRaises(CheckpointIOError):
            self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="split")
        checkpoint = self.lab.store.checkpoint("north")
        self.assertEqual(checkpoint["apply_ahead"], 1)
        self.assertTrue(checkpoint["applied_token"])
        self.assertEqual(self.lab.store.ledger_count(PARENT), 4)
        snapshots_before = checkpoint["snapshot_id"]
        report = self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="split")
        self.assertTrue(report.replayed)
        self.assertEqual(report.seen_ids, ["lot-04", "lot-05", "lot-06", "lot-07"])
        self.assertEqual(report.snapshot_id, snapshots_before)
        counts = Counter(resource_id for _parent, resource_id in self.lab.store.executions)
        self.assertEqual(len(counts), 6)
        self.assertTrue(all(value == 1 for value in counts.values()))
        upserts = self.lab.store.completed_upserts_by_resource(PARENT)
        self.assertEqual(set(upserts.values()), {1})
        self.assertEqual(self.lab.store.checkpoint("north")["done"], 1)

    def test_atomic_checkpoint_error_rolls_the_page_back(self) -> None:
        self.lab.store.fail_next_checkpoint = True
        with self.assertRaises(CheckpointIOError):
            self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="atomic")
        self.assertTrue(self.lab.journal.has("checkpoint_io", "surface_error"))
        # The page upserts ran inside the transaction before the barrier failed.
        self.assertEqual(self.lab.store.idempotency_lookups, 2)
        self.assertEqual(self.lab.store.ledger_count(PARENT), 0)
        self.assertEqual(self.lab.store.executions, [])
        self.assertEqual(self.lab.store.completed_upserts_by_resource(PARENT), {})
        checkpoint = self.lab.store.checkpoint("north")
        self.assertIsNone(checkpoint["next_token"])
        self.assertEqual(checkpoint["apply_ahead"], 0)
        report = self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="atomic")
        self.assertFalse(report.replayed)
        self.assertEqual(self.lab.store.ledger_count(PARENT), 6)
        counts = Counter(resource_id for _parent, resource_id in self.lab.store.executions)
        self.assertTrue(all(value == 1 for value in counts.values()))

    def test_expired_lease_fences_the_old_owner(self) -> None:
        fp = param_fingerprint(PARENT, "note=open", "sort_key")
        store = self.lab.store
        self.assertTrue(store.try_acquire("north", "worker-a", self.lab.clock.now(), 60, fp))
        self.assertFalse(store.try_acquire("north", "worker-b", self.lab.clock.now(), 60, fp))
        self.lab.clock.advance(61)
        self.assertTrue(store.try_acquire("north", "worker-b", self.lab.clock.now(), 60, fp))
        self.assertFalse(store.renew_lease("north", "worker-a", self.lab.clock.now(), 60))
        with self.assertRaises(LeaseDenied):
            store.write_barrier("north", "worker-a", "", "tok", "snap-x")
        with self.assertRaises(LeaseDenied):
            store.apply_atomic(
                "north", "worker-a", helpers.CALLER, PARENT, "snap-x",
                [helpers.lot("lot-01", 1)], "", "tok", self.lab.clock.now(),
            )
        self.assertEqual(store.ledger_count(PARENT), 0)
        self.assertIsNone(store.checkpoint("north")["next_token"])
        self.assertTrue(store.renew_lease("north", "worker-b", self.lab.clock.now(), 60))

    def test_changed_parameters_on_an_existing_scope_are_rejected(self) -> None:
        self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2, mode="atomic", max_pages=1)
        with self.assertRaises(CheckpointMismatch):
            self.lab.worker.run("north", PARENT, "", "sort_key", 2, mode="atomic")
        self.assertEqual(self.lab.store.ledger_count(PARENT), 2)

    def test_duplicate_metric_sees_a_second_snapshot_execution(self) -> None:
        # Guards the CLI metric: replaying a page under a new snapshot id would
        # derive new keys, and the per-resource completed count would reach 2.
        row = helpers.lot("lot-01", 1)
        with self.lab.store.lock:
            for snapshot_id in ("snap-one", "snap-two"):
                self.lab.store.begin()
                self.lab.store.apply_rows(helpers.CALLER, PARENT, snapshot_id, [row], self.lab.clock.now())
                self.lab.store.commit()
        self.assertEqual(self.lab.store.ledger_count(PARENT), 1)
        self.assertEqual(self.lab.store.completed_upserts_by_resource(PARENT), {"lot-01": 2})

    def test_dry_run_writes_no_ledger_row(self) -> None:
        self.lab.worker.dry_run = True
        report = self.lab.worker.run("north", PARENT, "note=open", "sort_key", 2)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.seen_ids, ["lot-01", "lot-02", "lot-04", "lot-05", "lot-06", "lot-07"])
        self.assertEqual(self.lab.store.ledger_count(), 0)
        self.assertIsNone(self.lab.store.checkpoint("north"))


if __name__ == "__main__":
    unittest.main()
