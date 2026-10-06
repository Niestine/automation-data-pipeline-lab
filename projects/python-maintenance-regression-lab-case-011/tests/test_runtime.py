"""Builtin windows, fork, refcounts, and the GIL quantum."""

from __future__ import annotations

import json
import unittest

from support import execute, scenario
from yieldlab.errors import (
    Deadlock,
    Invariant,
    LabError,
    ReplayDivergence,
    UseAfterFree,
    WrongThread,
)
from yieldlab.scenarios import build


class RuntimeTests(unittest.TestCase):
    def test_sort_window_and_external_lock(self) -> None:
        opened = scenario(
            "sort",
            {
                1: [("sort_begin", "nums"), ("sort_end", "nums")],
                2: [("list_len", "nums", "seen"), ("list_get", "nums", "head")],
            },
            cells={"seen": -1, "head": "unset"},
            extra={"lists": {"nums": [3, 1, 2]}},
        )
        trace = execute(opened, policy="guide", guide=[1, 2, 2, 1])
        self.assertEqual(trace.cells["seen"], 0)
        self.assertIsNone(trace.cells["head"])
        self.assertEqual(trace.extra["lists"]["nums"], [1, 2, 3])

        # Depth-2 PCT seeds can preempt inside sort. The unlocked reader must
        # land in the window on some seed, or the locked loop below is vacuous.
        bare = scenario(
            "sort_bare",
            {
                1: [("sort_begin", "nums"), ("sort_end", "nums")],
                2: [("list_len", "nums", "seen")],
            },
            cells={"seen": -1},
            extra={"lists": {"nums": [3, 1, 2]}},
        )
        bare_seen = {
            execute(bare, seed=seed, depth=2, n_max=2, k_budget=8).cells["seen"]
            for seed in range(40)
        }
        self.assertIn(0, bare_seen)

        closed = scenario(
            "sort_locked",
            {
                1: [("acq", "nums"), ("sort_begin", "nums"), ("sort_end", "nums"), ("rel", "nums")],
                2: [("acq", "nums"), ("list_len", "nums", "seen"), ("rel", "nums")],
            },
            cells={"seen": -1},
            extra={"lists": {"nums": [3, 1, 2]}},
        )
        for seed in range(40):
            locked = execute(closed, seed=seed, depth=2, n_max=2, k_budget=8)
            self.assertEqual(locked.cells["seen"], 3)
            self.assertEqual(locked.extra["lists"]["nums"], [1, 2, 3])

    def test_memoryview_tear_lock_and_resize(self) -> None:
        buf = {"bufs": {"b": bytearray(4)}, "views": {"b": 0}}
        torn = scenario(
            "view",
            {
                1: [("view_write", "b", 0, 0x11), ("view_write", "b", 1, 0x11)],
                2: [("view_write", "b", 2, 0x22), ("view_write", "b", 3, 0x22)],
                3: [("snap_buf", "b", "shot")],
            },
            extra=buf,
        )
        trace = execute(torn, policy="guide", guide=[1, 2, 3, 1, 2])
        self.assertEqual(trace.extra["shot"], b"\x11\x00\x22\x00")
        self.assertEqual(bytes(trace.extra["bufs"]["b"]), b"\x11\x11\x22\x22")

        # Both writers and the reader take one external lock. Across seeded
        # depth-3 schedules the snapshot is always a whole-buffer state. The
        # same seeds without the lock tear, so the locked loop is not vacuous.
        whole_states = {b"\x00" * 4, b"\x11" * 4, b"\x22" * 4}

        def bytes_of(value: int) -> list:
            return [("view_write", "b", i, value) for i in range(4)]

        bare = scenario(
            "view_bare",
            {1: bytes_of(0x11), 2: bytes_of(0x22), 3: [("snap_buf", "b", "shot")]},
            extra={"bufs": {"b": bytearray(4)}, "views": {"b": 0}},
        )
        bare_shots = {
            execute(bare, seed=seed, depth=3, n_max=3, k_budget=16).extra["shot"]
            for seed in range(60)
        }
        self.assertTrue(bare_shots - whole_states)

        def writer(value: int) -> list:
            return [("acq", "buf"), *bytes_of(value), ("rel", "buf")]

        whole = scenario(
            "view_locked",
            {
                1: writer(0x11),
                2: writer(0x22),
                3: [("acq", "buf"), ("snap_buf", "b", "shot"), ("rel", "buf")],
            },
            extra={"bufs": {"b": bytearray(4)}, "views": {"b": 0}},
        )
        shots = set()
        for seed in range(60):
            locked = execute(whole, seed=seed, depth=3, n_max=3, k_budget=16)
            shots.add(locked.extra["shot"])
        self.assertLessEqual(shots, whole_states)
        self.assertGreater(len(shots), 1)

        resize = scenario(
            "resize",
            {1: [("export_view", "b"), ("resize", "b", 8)]},
            extra={"bufs": {"b": bytearray(b"xy")}, "views": {"b": 0}},
        )
        with self.assertRaises(BufferError):
            execute(resize)

    def test_custom_eq_drops_the_per_object_lock(self) -> None:
        case = scenario(
            "eq",
            {1: [("const", 5), ("dict_set_custom", "box")]},
            extra={"dicts": {"box": {}}},
        )
        trace = execute(case)
        self.assertIn("lock_released_for_eq", trace.marks)
        self.assertEqual(trace.extra["dicts"]["box"]["from_eq"], 1)
        self.assertEqual(trace.extra["dicts"]["box"]["custom"], 5)

        held = scenario(
            "eq_held",
            {1: [("const", 5), ("dict_set_custom", "box")]},
            extra={"dicts": {"box": {}}},
            release_for_eq=False,
        )
        with self.assertRaises(Deadlock):
            execute(held)

        plain = scenario(
            "eq_str",
            {1: [("const", 5), ("dict_set_str", "box", "a")]},
            extra={"dicts": {"box": {}}},
        )
        trace = execute(plain)
        self.assertIn("eq_under_lock", trace.marks)
        self.assertNotIn("lock_released_for_eq", trace.marks)

    def test_dict_check_then_delete_crashes_and_pop_does_not(self) -> None:
        pair = [("dict_contains", "box", "k"), ("dict_del", "box", "k")]
        broken = scenario(
            "cta_threads",
            {1: list(pair), 2: [("dict_contains", "box", "k")]},
            extra={"dicts": {"box": {"k": 1}}},
        )
        execute(broken, policy="guide", guide=[1, 1, 2])
        racing = scenario(
            "cta_race",
            {1: list(pair), 2: list(pair)},
            extra={"dicts": {"box": {"k": 1}}},
        )
        # Both threads pass the check before either deletes.
        with self.assertRaises(Invariant) as caught:
            execute(racing, policy="guide", guide=[1, 2, 1, 2])
        self.assertIn("KeyError", str(caught.exception))

        fixed = scenario(
            "pop_threads",
            {1: [("dict_pop", "box", "k")], 2: [("dict_pop", "box", "k")]},
            extra={"dicts": {"box": {"k": 1}}},
        )
        trace = execute(fixed, policy="guide", guide=[1, 2])
        self.assertEqual(trace.extra["dicts"]["box"], {})

    def test_dict_write_is_one_step(self) -> None:
        case = scenario(
            "dict_step",
            {
                1: [("const", 7), ("dict_set", "box", "k")],
                2: [("dict_get", "box", "k"), ("note", "read")],
            },
            extra={"dicts": {"box": {"k": 0}}},
        )
        before = execute(case, policy="guide", guide=[2, 1, 1])
        self.assertEqual(before.extra["acc"][2], 0)
        after = execute(case, policy="guide", guide=[1, 1, 2])
        self.assertEqual(after.extra["acc"][2], 7)
        self.assertEqual(len([e for e in after.events if e["op"] == "dict_set"]), 1)

        # d[k] = d[k] + 1 is three steps. Each dict call is safe; the
        # statement is not, and the interleaving loses one increment.
        bump = [("dict_get", "box", "k"), ("add", 1), ("dict_set", "box", "k")]
        statement = scenario(
            "dict_rmw", {1: list(bump), 2: list(bump)}, extra={"dicts": {"box": {"k": 0}}}
        )
        serial = execute(statement, policy="guide", guide=[1, 1, 1, 2, 2, 2])
        self.assertEqual(serial.extra["dicts"]["box"]["k"], 2)
        torn = execute(statement, policy="guide", guide=[1, 2, 1, 2, 1, 2])
        self.assertEqual(torn.extra["dicts"]["box"]["k"], 1)

    def test_fork_resets_python_locks_only(self) -> None:
        case = scenario(
            "fork",
            {
                1: [("wait_ready",), ("fork_os",), ("acq", "py", "python"), ("note", "py_ok"), ("acq", "np", "native")],
                2: [("acq", "py", "python"), ("acq", "np", "native"), ("ready",), ("park",)],
            },
            invariant_id="fork_locks",
        )
        with self.assertRaises(Deadlock) as caught:
            execute(case, seed=1)
        events = caught.exception.journal["events"]
        self.assertIn("py_ok", [arg for event in events for arg in event["args"]])
        self.assertIn("fork_os", [event["op"] for event in events])
        after_fork = events[[event["op"] for event in events].index("fork_os"):]
        self.assertEqual({event["thread"] for event in after_fork}, {1})

        off_main = scenario("fork_off_main", {1: [("bc",)], 2: [("fork_os",)]})
        with self.assertRaises(Invariant):
            execute(off_main, policy="guide", guide=[2])

    def test_allow_threads_releases_the_gil(self) -> None:
        held = scenario(
            "gil_held",
            {1: [("bc", "a"), ("bc", "b"), ("bc", "c")], 2: [("bc", "partner")]},
        )
        with self.assertRaises(ReplayDivergence):
            execute(held, mode="gil", switch_interval=100, policy="guide", guide=[1, 2])
        released = scenario(
            "gil_allow",
            {1: [("bc", "a"), ("allow",), ("bc", "c")], 2: [("bc", "partner")]},
        )
        trace = execute(released, mode="gil", switch_interval=100, policy="guide", guide=[1, 1, 2, 1])
        self.assertEqual([event["thread"] for event in trace.events], [1, 1, 2, 1])

    def test_restore_and_ensure(self) -> None:
        restore = scenario("restore", {1: [("restore",)]})
        with self.assertRaises(Deadlock):
            execute(restore)
        ensure = scenario("ensure", {1: [("ensure",)]}, finalizing=True)
        with self.assertRaises(WrongThread):
            execute(ensure)

    def test_plain_shared_increment_frees_a_live_ref(self) -> None:
        case = build("refcount", "unfixed")
        # Serial orders are correct: the bug needs an interleaving.
        for guide in ([1, 1, 1, 1, 2, 2, 2, 2], [2, 2, 2, 2, 1, 1, 1, 1]):
            trace = execute(case, policy="guide", guide=guide, n_max=4)
            self.assertEqual(trace.extra["headers"]["obj"]["shared"], 1)
        # Both reads before both writes lose one increment. Thread 2's
        # decref then drops the count to zero while it still holds a ref.
        with self.assertRaises(UseAfterFree):
            execute(case, policy="guide", guide=[1, 2, 1, 2, 1, 1, 2, 2], n_max=4)

    def test_atomic_shared_increment_is_exact(self) -> None:
        unfixed = build("refcount", "unfixed")
        fixed = build("refcount", "fixed")
        unfixed_hits = 0
        for seed in range(80):
            try:
                execute(unfixed, seed=seed, depth=3, n_max=2, k_budget=8)
            except LabError:
                unfixed_hits += 1
            trace = execute(fixed, seed=seed, depth=3, n_max=2, k_budget=8)
            header = trace.extra["headers"]["obj"]
            self.assertEqual(header["shared"], 1)
            self.assertEqual(header["state"], "weakrefs")
            self.assertTrue(header["alive"])
        self.assertGreater(unfixed_hits, 0)

    def test_immortal_ignores_traffic(self) -> None:
        case = scenario(
            "immortal",
            {
                1: [
                    ("incref_local", "obj"),
                    ("incref_shared_atomic", "obj"),
                    ("decref_shared", "obj"),
                ]
            },
            extra={"headers": {"obj": _header(immortal=True)}},
        )
        trace = execute(case)
        header = trace.extra["headers"]["obj"]
        self.assertEqual(header["local"], 0)
        self.assertEqual(header["shared"], 0)
        self.assertTrue(header["alive"])
        self.assertEqual(trace.marks.count("immortal_noop"), 3)

    def test_dead_owner_merges_on_the_acting_thread(self) -> None:
        case = scenario(
            "dead_owner",
            {1: [("decref_shared", "obj")]},
            extra={"headers": {"obj": _header(owner=4, owner_alive=False, local=5)}},
        )
        trace = execute(case)
        header = trace.extra["headers"]["obj"]
        self.assertEqual(header["local"], 4)
        self.assertEqual(header["shared"], 0)
        self.assertEqual(header["state"], "merged")
        self.assertIn("merge_actor:1", trace.marks)

    def test_queued_merge_runs_during_the_pause(self) -> None:
        case = scenario(
            "queued",
            {1: [("stw",)], 2: [("decref_shared", "obj"), ("stw",)]},
            extra={"headers": {"obj": _header(local=1)}},
        )
        trace = execute(case, policy="guide", guide=[2, 1, 2])
        header = trace.extra["headers"]["obj"]
        # The owner is alive, so the negative shared count waits for the pause.
        self.assertEqual(header["state"], "merged")
        self.assertEqual((header["local"], header["shared"]), (0, 0))
        self.assertFalse(header["alive"])
        self.assertLess(trace.marks.index("merge_actor:2"), trace.marks.index("stw_release"))

    def test_finalizer_runs_after_the_pause(self) -> None:
        broken = build("finalizer_stw", "unfixed")
        with self.assertRaises(Deadlock) as caught:
            execute(broken, seed=0, n_max=9, k_budget=16)
        blob = json.dumps(caught.exception.journal["events"])
        self.assertNotIn("finalized", blob)

        fixed = build("finalizer_stw", "fixed")
        trace = execute(fixed, seed=0, n_max=9, k_budget=16)
        self.assertIn("stw_release", trace.marks)
        self.assertLess(trace.marks.index("stw_release"), trace.marks.index("finalized"))
        self.assertNotIn("finalized_in_stw", trace.marks)


def _header(**overrides) -> dict:
    header = {
        "owner": 1,
        "owner_alive": True,
        "local": 0,
        "shared": 0,
        "immortal": False,
        "state": "default",
        "alive": True,
        "finalizer": None,
        "deferred": False,
    }
    header.update(overrides)
    return header


if __name__ == "__main__":
    unittest.main()
