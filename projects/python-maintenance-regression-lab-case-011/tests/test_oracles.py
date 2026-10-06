"""FastTrack epochs and Lipton reduction.

Lock clocks join on release into a later acquire of the same lock. That edge
orders only what follows the acquire: a write placed before the acquire, or a
critical section on a different lock, stays concurrent and is reported.
"""

from __future__ import annotations

import unittest

from support import execute, scenario
from yieldlab.engine import Schedule, epoch_ratio, run
from yieldlab.errors import AtomicityViolation, LabError, Race
from yieldlab.scenarios import build


class OracleTests(unittest.TestCase):
    def test_release_acquire_orders_the_cell(self) -> None:
        case = scenario(
            "lock_pub",
            {
                1: [("acq", "gate"), ("write", "x", 1), ("rel", "gate")],
                2: [("acq", "gate"), ("read", "x", "seen"), ("rel", "gate")],
            },
            cells={"x": 0, "seen": -1},
            holds=lambda world: world.cells["seen"] == 1,
        )
        trace = execute(case, policy="guide", guide=[1, 1, 1, 2, 2, 2])
        self.assertEqual(trace.races, [])
        self.assertEqual(trace.cells["seen"], 1)

    def test_acquire_does_not_hide_a_later_write(self) -> None:
        case = scenario(
            "lock_gap",
            {
                1: [("acq", "gate"), ("read", "x", "seen"), ("rel", "gate")],
                2: [("write", "x", 1), ("acq", "gate"), ("rel", "gate")],
            },
            cells={"x": 0, "seen": -1},
        )
        with self.assertRaises(Race):
            execute(
                case,
                policy="guide",
                guide=[1, 1, 1, 2, 2, 2],
                raise_races=True,
            )

    def test_a_different_lock_does_not_order_the_cell(self) -> None:
        case = scenario(
            "two_locks",
            {
                1: [("acq", "a"), ("write", "x", 1), ("rel", "a")],
                2: [("acq", "b"), ("read", "x", "seen"), ("rel", "b")],
            },
            cells={"x": 0, "seen": -1},
        )
        trace = execute(case, policy="guide", guide=[1, 1, 1, 2, 2, 2])
        self.assertEqual(trace.races, ["read cell:x by 2 races with write by 1"])
        # Every access held some lock, but no single lock covers both.
        self.assertIn("cell:x", trace_warnings(trace))

    def test_fork_join_is_not_a_lockset_failure(self) -> None:
        case = scenario(
            "fork_join",
            {
                1: [("write", "y", 1), ("fork", 2), ("join", 2), ("read", "x", "seen")],
                2: [("read", "y", "from_parent"), ("write", "x", 7)],
            },
            unborn=frozenset({2}),
            cells={"x": 0, "y": 0, "seen": -1, "from_parent": -1},
            holds=lambda world: world.cells["seen"] == 7 and world.cells["from_parent"] == 1,
        )
        trace = execute(case, policy="guide", guide=[1, 1, 2, 2, 1, 1], seed=0)
        self.assertEqual(trace.races, [])
        self.assertIn("cell:x", trace_warnings(trace))
        self.assertIn("cell:y", trace_warnings(trace))

    def test_read_shared_cell_widens_then_collapses(self) -> None:
        # Two forked children read x concurrently: the read state widens to a
        # vector. After both joins the parent's write is ordered with both
        # reads, collapses x back to an epoch, and a later read is epoch-only.
        writes = [("write", "pad", 1) for _ in range(20)]
        case = scenario(
            "epochs",
            {
                1: [
                    ("write", "x", 1),
                    ("fork", 2),
                    ("fork", 3),
                    *writes,
                    ("join", 2),
                    ("join", 3),
                    ("write", "x", 2),
                    ("read", "x", "s1"),
                ],
                2: [("read", "x", "s2")],
                3: [("read", "x", "s3")],
            },
            unborn=frozenset({2, 3}),
            cells={"x": 0, "pad": 0, "s1": -1, "s2": -1, "s3": -1},
        )
        guide = [1, 1, 1, 2, 3, *([1] * 20), 1, 1, 1, 1]
        trace = execute(case, policy="guide", guide=guide, n_max=4)
        self.assertEqual(trace.races, [])
        # One vector read when thread 3 joins thread 2's read, one vector
        # check on the collapsing write. Everything else is an epoch.
        self.assertEqual(trace.vector_ops, 2)
        self.assertGreaterEqual(epoch_ratio(trace), 0.90)

    def test_catalog_cells_stay_on_the_epoch_path(self) -> None:
        epoch = vector = 0
        for name, variant in (
            ("ordering_d1", "unfixed"),
            ("ordering_d1", "fixed"),
            ("lost_update", "unfixed"),
            ("lost_update", "locked"),
        ):
            for seed in range(40):
                schedule = Schedule(seed=seed, depth=2, n_max=4, k_budget=16)
                try:
                    trace = run(build(name, variant), schedule)
                except LabError:
                    continue
                epoch += trace.epoch_ops
                vector += trace.vector_ops
        self.assertGreater(epoch, 0)
        self.assertGreaterEqual(epoch / (epoch + vector), 0.90)

    def test_stringbuffer_gap_fails_on_a_serial_run(self) -> None:
        case = scenario(
            "append",
            {
                1: [
                    ("atomic", "append"),
                    ("buf_length", "b"),
                    ("buf_getchars", "b"),
                    ("atomic_end",),
                ]
            },
            extra={"bufs": {"b": bytearray(b"abcd")}},
        )
        with self.assertRaises(AtomicityViolation):
            execute(case, raise_atomicity=True)

    def test_stringbuffer_under_one_lock_reduces(self) -> None:
        case = scenario(
            "append_locked",
            {
                1: [
                    ("atomic", "append"),
                    ("acq", "buf"),
                    ("buf_length", "b"),
                    ("buf_getchars", "b"),
                    ("rel", "buf"),
                    ("atomic_end",),
                ]
            },
            extra={"bufs": {"b": bytearray(b"abcd")}},
        )
        trace = execute(case, raise_atomicity=True)
        self.assertEqual(trace.violations, [])

    def test_dict_check_then_delete_and_pop(self) -> None:
        broken = scenario(
            "cta",
            {
                1: [
                    ("atomic", "cta"),
                    ("dict_contains", "box", "k"),
                    ("dict_del", "box", "k"),
                    ("atomic_end",),
                ]
            },
            extra={"dicts": {"box": {"k": 1}}},
        )
        with self.assertRaises(AtomicityViolation):
            execute(broken, raise_atomicity=True)
        fixed = scenario(
            "pop",
            {1: [("atomic", "pop"), ("dict_pop", "box", "k"), ("atomic_end",)]},
            extra={"dicts": {"box": {"k": 1}}},
        )
        trace = execute(fixed, raise_atomicity=True)
        self.assertEqual(trace.extra["dicts"]["box"], {})
        self.assertEqual(trace.violations, [])

    def test_set_contains_then_remove(self) -> None:
        broken = scenario(
            "set_cta",
            {
                1: [
                    ("atomic", "set"),
                    ("set_contains", "s", "a"),
                    ("set_remove", "s", "a"),
                    ("atomic_end",),
                ]
            },
            extra={"sets": {"s": {"a", "b"}}},
        )
        with self.assertRaises(AtomicityViolation):
            execute(broken, raise_atomicity=True)
        fixed = scenario(
            "discard",
            {1: [("atomic", "discard"), ("set_discard", "s", "a"), ("atomic_end",)]},
            extra={"sets": {"s": {"a", "b"}}},
        )
        trace = execute(fixed, raise_atomicity=True)
        self.assertNotIn("a", trace.extra["sets"]["s"])

    def test_right_mover_after_release_is_rejected(self) -> None:
        case = scenario(
            "relock",
            {1: [("atomic", "relock"), ("acq", "m"), ("rel", "m"), ("acq", "m"), ("atomic_end",)]},
        )
        with self.assertRaises(AtomicityViolation):
            execute(case, raise_atomicity=True)

    def test_unsynchronized_rmw_is_not_atomic(self) -> None:
        case = scenario(
            "rmw",
            {
                1: [
                    ("atomic", "inc"),
                    ("load", "count"),
                    ("add", 1),
                    ("store", "count"),
                    ("atomic_end",),
                ]
            },
            cells={"count": 0},
        )
        with self.assertRaises(AtomicityViolation):
            execute(case, raise_atomicity=True)


def trace_warnings(trace) -> list[str]:
    # Eraser-style lockset warnings computed beside FastTrack. They are a hint
    # for the reader, never the pass/fail signal.
    return trace.extra["lockset_warnings"]


if __name__ == "__main__":
    unittest.main()
