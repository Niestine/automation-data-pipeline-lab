"""Nested rollback compensates each forward update once, including a crash during restart."""

from __future__ import annotations

import unittest

import helpers as _path  # noqa: F401  (puts src/ on sys.path)

from prefixlab.recover import restart
from prefixlab.txn import Store
from prefixlab.wal import compensation_counts


def _nested(crash_after: int | None = None) -> Store:
    store = Store()
    store.open_page("page", b"ORIG")
    store.begin("T")
    store.update("T", "page", b"v1")
    store.savepoint("T")
    store.update("T", "page", b"v2")
    store.savepoint("T")
    store.update("T", "page", b"v3")
    store.rollback_to("T")
    store.update("T", "page", b"v4")
    finished = store.rollback_to("T", crash_after=crash_after)
    if crash_after is None:
        assert finished
    return store


class SavepointTests(unittest.TestCase):
    def test_committed_nested_rollback_keeps_the_outer_image(self):
        store = _nested()
        self.assertEqual(store.page("page"), b"v1")
        store.update("T", "page", b"v1+later")
        store.commit("T")
        self.assertEqual(store.page("page"), b"v1+later")
        self.assertNotIn(b"v3", store.page("page"))
        self.assertTrue(all(count == 1 for count in compensation_counts(store.log.records).values()))

    def test_crash_inside_nested_rollback_then_again_during_restart(self):
        store = _nested(crash_after=1)
        self.assertEqual(store.txns["T"].state, "active")
        initial = {"page": b"ORIG"}
        partial = restart(initial, list(store.log.records), crash_after=1)
        self.assertLessEqual(max(compensation_counts(partial.log.records).values()), 1)
        self.assertNotIn("abort", [record.kind for record in partial.log.records if record.txn == "T"][-1:])
        finished = restart(initial, list(partial.log.records))
        self.assertEqual(finished.page("page"), b"ORIG")
        counts = compensation_counts(finished.log.records)
        self.assertTrue(counts)
        self.assertTrue(all(count == 1 for count in counts.values()))
        for lsn, count in compensation_counts(partial.log.records).items():
            self.assertEqual(counts[lsn], count)
        again = restart(initial, list(finished.log.records))
        self.assertEqual(len(again.log.records), len(finished.log.records))
        self.assertEqual(compensation_counts(again.log.records), counts)


if __name__ == "__main__":
    unittest.main()
