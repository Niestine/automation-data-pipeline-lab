"""Two decrements of 3 against original stock 5."""

from __future__ import annotations

import unittest

from helpers import capture_logger

from prefixlab.errors import OracleFault
from prefixlab.history import check_history
from prefixlab.inventory import (
    Shelf,
    decrement_if_enough,
    drive,
    fault_if_lost,
    run_both_reads_first,
    run_exclusive_abort,
    run_serial,
)


class InventoryTests(unittest.TestCase):
    def test_serial_accepts_one_call_in_every_mode(self):
        for mode in ("nolock", "txn_read_committed", "exclusive"):
            run = run_serial(mode)
            self.assertEqual(run.stock.total, 5)
            self.assertEqual(run.stock.consumed, 3)
            self.assertEqual(sum(call.accepted for call in run.calls), 1)

    def test_nolock_interleaving_breaks_the_sum(self):
        run = run_both_reads_first("nolock")
        self.assertEqual(run.stock.total, 8)
        self.assertEqual(sum(call.accepted for call in run.calls), 2)
        log, handler, lines = capture_logger()
        try:
            with self.assertRaises(OracleFault) as caught:
                fault_if_lost(run)
        finally:
            log.removeHandler(handler)
        payload = caught.exception.payload
        self.assertEqual(payload["outcome"], "G2")
        self.assertEqual(payload["cut"], "both_reads_first")
        self.assertEqual(set(payload["transactions"]), {"c1", "c2"})
        self.assertEqual(payload["missing"], ["c1-tok"])
        self.assertIn("outcome=G2 cut=both_reads_first transactions=c1,c2", lines)

    def test_same_schedule_fails_unlocked_and_passes_with_the_write_lock(self):
        for mode in ("nolock", "txn_read_committed"):
            with self.assertRaises(OracleFault, msg=mode):
                fault_if_lost(run_both_reads_first(mode))
        self.assertIsNone(fault_if_lost(run_both_reads_first("exclusive"))["anomaly"])

    def test_read_committed_buffers_its_write_until_commit(self):
        shelf = Shelf(5)
        call = decrement_if_enough(shelf, "c1", 3, "txn_read_committed")
        self.assertEqual(next(call), "after_read")
        self.assertEqual(next(call), "after_write")
        self.assertEqual((shelf.stock.remaining, shelf.stock.consumed, shelf.stock.tokens), (5, 0, []))
        (result,) = drive(shelf, {"c1": call}, [])
        self.assertTrue(result.accepted)
        self.assertEqual((shelf.stock.remaining, shelf.stock.consumed), (2, 3))

    def test_exclusive_lock_cycle_is_reported_as_deadlock(self):
        shelf = Shelf(5)
        shelf.try_lock("outsider")
        with self.assertRaises(RuntimeError):
            drive(shelf, {"c1": decrement_if_enough(shelf, "c1", 3, "exclusive")}, [])

    def test_read_committed_without_a_write_lock_also_breaks_the_sum(self):
        run = run_both_reads_first("txn_read_committed")
        self.assertEqual(run.stock.total, 8)
        self.assertIn("c1.begin", run.events)
        self.assertIn("c2.commit", run.events)
        self.assertEqual(sum(call.accepted for call in run.calls), 2)

    def test_exclusive_interleaving_keeps_the_sum(self):
        run = run_both_reads_first("exclusive")
        self.assertEqual(run.stock.total, 5)
        self.assertEqual(run.stock.consumed, 3)
        self.assertEqual(sum(call.accepted for call in run.calls), 1)
        self.assertLess(run.events.index("c2.blocked"), run.events.index("c2.after_read"))
        self.assertLess(run.events.index("c1.after_read"), run.events.index("c2.blocked"))
        self.assertLess(run.events.index("c1.commit"), run.events.index("c2.after_read"))
        report = fault_if_lost(run)
        self.assertIsNone(report["anomaly"])
        self.assertEqual(report["missing"], [])

    def test_aborted_exclusive_decrement_is_invisible(self):
        run, while_locked, views = run_exclusive_abort()
        self.assertTrue(while_locked["blocked"])
        self.assertEqual(views["holder"]["tokens"], ["c1-tok"])
        self.assertEqual(run.stock.total, 5)
        self.assertEqual(run.stock.remaining, 5)
        self.assertEqual(run.stock.consumed, 0)
        self.assertEqual(run.stock.tokens, [])
        self.assertEqual(views["after"]["tokens"], [])
        self.assertFalse(views["after"]["blocked"])
        dirty = {
            "transactions": [
                {
                    "id": "c1",
                    "status": "aborted",
                    "ops": [{"op": "append", "key": "stock", "tokens": ["c1-tok"], "value": ["c1-tok"]}],
                },
                {
                    "id": "reader",
                    "status": "committed",
                    "ops": [{"op": "read", "key": "stock", "value": ["c1-tok"]}],
                },
            ],
            "surviving": {"stock": []},
        }
        report = check_history(dirty)
        self.assertEqual(report["anomaly"], "G1a")
        self.assertEqual(set(report["transactions"]), {"c1", "reader"})

    def test_bad_amount_and_mode_are_refused(self):
        with self.assertRaises(ValueError):
            run_serial("nolock", amount=0)
        with self.assertRaises(ValueError):
            run_serial("immediate")
        with self.assertRaises(ValueError):
            decrement_if_enough(Shelf(5), "c1", 3, "nolock", abort=True)


if __name__ == "__main__":
    unittest.main()
