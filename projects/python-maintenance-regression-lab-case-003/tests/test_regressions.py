"""Checked-in schedules: fail on the buggy twin, pass or diverge on the fix."""

from __future__ import annotations

import json
import helpers  # noqa: F401
import unittest
from pathlib import Path

from schedlab.artifact import ArtifactError, load, validate
from schedlab.oracles import data_race_replay_ok, deadlock_replay_ok
from schedlab.policies import run_fair, run_pct, search_schedules
from schedlab.replay import replay
from schedlab.subjects import atomicity_d2, deadlock_d2, ordering_d1

EXAMPLES = helpers.EXAMPLES


def _replay_fixture(path: Path, revision: str):
    payload = load(path)
    subject = {
        "ordering_d1": ordering_d1,
        "atomicity_d2": atomicity_d2,
        "deadlock_d2": deadlock_d2,
    }[payload["subject"]](revision)
    machine = replay(subject, payload["schedule"], n_max=payload["n_max"], k=payload["k"])
    return payload, machine


class ReplayIdentityTests(unittest.TestCase):
    def test_same_schedule_twice(self) -> None:
        payload = load(EXAMPLES / "atomicity_d2_buggy.json")
        subject = atomicity_d2("buggy")
        first = replay(subject, payload["schedule"], n_max=2, k=4)
        second = replay(subject, payload["schedule"], n_max=2, k=4)
        self.assertEqual(first.terminal, second.terminal)
        self.assertEqual(first.schedule, second.schedule)
        self.assertEqual(first.cells, second.cells)
        self.assertEqual(first.thread_locals(), second.thread_locals())
        self.assertEqual(first.max_reentrancy, 1)
        self.assertEqual(second.max_reentrancy, 1)
        before = (EXAMPLES / "atomicity_d2_buggy.json").read_bytes()
        replay(subject, payload["schedule"], n_max=2, k=4)
        self.assertEqual((EXAMPLES / "atomicity_d2_buggy.json").read_bytes(), before)

    def test_disabled_third_tid_diverges_and_fails_the_data_race_gate(self) -> None:
        machine = replay(atomicity_d2("fixed"), [1, 2, 9], n_max=2, k=4)
        self.assertEqual(machine.terminal, "diverged")
        self.assertFalse(data_race_replay_ok("fail", machine.terminal))


class OrderingTests(unittest.TestCase):
    def test_checked_in_schedule(self) -> None:
        payload, buggy = _replay_fixture(EXAMPLES / "ordering_d1_buggy.json", "buggy")
        self.assertEqual(payload["schedule"], [2, 2, 1, 1])
        self.assertEqual(payload["terminal"], "fail")
        self.assertEqual(buggy.terminal, "fail")
        self.assertEqual(buggy.threads[2].locals["observed"], 0)
        _payload, fixed = _replay_fixture(EXAMPLES / "ordering_d1_buggy.json", "fixed")
        self.assertEqual(fixed.terminal, "pass")
        self.assertEqual(fixed.threads[2].locals["observed"], "skip")
        self.assertTrue(data_race_replay_ok(buggy.terminal, fixed.terminal))

    def test_both_depth_one_priority_orders(self) -> None:
        seen: dict[tuple[int, ...], str] = {}
        for seed in range(80):
            trace = run_pct(ordering_d1("buggy"), seed=seed, n_max=2, k=4, d=1)
            self.assertEqual(trace.change_points, [])
            seen[tuple(trace.machine.schedule)] = trace.machine.terminal or ""
        self.assertEqual(seen[(1, 1, 2, 2)], "pass")
        self.assertEqual(seen[(2, 2, 1, 1)], "fail")
        fixed = replay(ordering_d1("fixed"), [2, 2, 1, 1], n_max=2, k=4)
        self.assertEqual(fixed.terminal, "pass")


class AtomicityTests(unittest.TestCase):
    def test_checked_in_schedule(self) -> None:
        payload, buggy = _replay_fixture(EXAMPLES / "atomicity_d2_buggy.json", "buggy")
        self.assertEqual(payload["schedule"], [1, 2, 1, 2])
        self.assertEqual(buggy.terminal, "fail")
        self.assertEqual(buggy.cells["x"], 1)
        _payload, fixed = _replay_fixture(EXAMPLES / "atomicity_d2_buggy.json", "fixed")
        self.assertEqual(fixed.terminal, "pass")
        self.assertEqual(fixed.cells["x"], 2)
        self.assertTrue(data_race_replay_ok(buggy.terminal, fixed.terminal))

    def test_depth_one_pct_misses_the_depth_two_bug(self) -> None:
        for seed in range(20):
            trace = run_pct(atomicity_d2("buggy"), seed=seed, n_max=2, k=4, d=1)
            self.assertEqual(trace.machine.terminal, "pass")
            self.assertIn(tuple(trace.machine.schedule), {(1, 1, 2, 2), (2, 2, 1, 1)})
            self.assertEqual(trace.machine.cells["x"], 2)

    def test_preemption_bound_finds_it_and_delay_bound_differs(self) -> None:
        buggy = atomicity_d2("buggy")
        clean = search_schedules(
            buggy, mode="preemption", bound=0, n_max=2, k=4, schedule_cap=40
        )
        self.assertIsNone(clean.failure)
        self.assertIn("no failure within bound 0", clean.coverage)
        found = search_schedules(
            buggy, mode="preemption", bound=1, n_max=2, k=4, schedule_cap=40
        )
        self.assertIsNotNone(found.failure)
        assert found.failure is not None
        self.assertEqual(found.failure.terminal, "fail")
        self.assertEqual(found.failure.schedule, [1, 2, 2, 1])
        self.assertEqual(found.failure.preemptions, 1)
        self.assertEqual(found.failure.cells["x"], 1)
        fixed = replay(atomicity_d2("fixed"), found.failure.schedule, n_max=2, k=4)
        self.assertEqual(fixed.terminal, "pass")
        self.assertEqual(fixed.cells["x"], 2)
        missed = search_schedules(
            buggy, mode="delay", bound=0, n_max=2, k=4, schedule_cap=40
        )
        self.assertIsNone(missed.failure)
        self.assertIn("no failure within bound 0", missed.coverage)
        self.assertNotIn("bug free", missed.coverage.lower())
        delayed = search_schedules(
            buggy, mode="delay", bound=1, n_max=2, k=4, schedule_cap=40
        )
        self.assertIsNotNone(delayed.failure)
        assert delayed.failure is not None
        self.assertEqual(delayed.failure.terminal, "fail")
        self.assertEqual(delayed.failure.schedule, [1, 2, 1, 2])
        self.assertEqual(delayed.failure.delays, 1)
        self.assertEqual(delayed.failure.cells["x"], 1)


class DeadlockTests(unittest.TestCase):
    def test_bound_zero_clean_and_bound_one_deadlocks(self) -> None:
        buggy = deadlock_d2("buggy")
        bound0 = search_schedules(
            buggy, mode="preemption", bound=0, n_max=2, k=8, schedule_cap=40,
            stop_on_failure=False,
        )
        self.assertEqual(bound0.terminals.get("deadlock", 0), 0)
        self.assertGreaterEqual(bound0.schedules_used, 1)
        bound1 = search_schedules(
            buggy, mode="preemption", bound=1, n_max=2, k=8, schedule_cap=40
        )
        self.assertIsNotNone(bound1.failure)
        assert bound1.failure is not None
        self.assertEqual(bound1.failure.terminal, "deadlock")
        self.assertEqual(bound1.failure.schedule, [1, 2, 2, 1])
        self.assertEqual(bound1.failure.preemptions, 1)
        payload, replayed = _replay_fixture(EXAMPLES / "deadlock_d2_buggy.json", "buggy")
        self.assertEqual(payload["schedule"], [1, 2, 2, 1])
        self.assertEqual(replayed.terminal, "deadlock")
        self.assertEqual(replayed.preemptions, 1)
        fixed_replay = replay(deadlock_d2("fixed"), payload["schedule"], n_max=2, k=8)
        self.assertEqual(fixed_replay.terminal, "diverged")
        delay0 = search_schedules(
            buggy, mode="delay", bound=0, n_max=2, k=8, schedule_cap=40, stop_on_failure=False
        )
        self.assertEqual(delay0.terminals.get("deadlock", 0), 0)
        self.assertEqual(delay0.completed[0].schedule[0], 1)
        delay1 = search_schedules(
            buggy, mode="delay", bound=1, n_max=2, k=8, schedule_cap=40
        )
        self.assertIsNotNone(delay1.failure)
        assert delay1.failure is not None
        self.assertEqual(delay1.failure.terminal, "deadlock")
        self.assertEqual(delay1.failure.delays, 1)

    def test_fixed_order_has_no_deadlock_through_bound_five(self) -> None:
        fixed = deadlock_d2("fixed")
        for bound in range(6):
            outcome = search_schedules(
                fixed,
                mode="preemption",
                bound=bound,
                n_max=2,
                k=8,
                schedule_cap=200,
                stop_on_failure=False,
            )
            self.assertFalse(outcome.hit_cap, msg=f"bound {bound} used {outcome.schedules_used}")
            self.assertEqual(outcome.terminals.get("deadlock", 0), 0)
            self.assertIn(f"no failure within bound {bound}", outcome.coverage)
            self.assertNotIn("bug free", outcome.coverage.lower())
        self.assertTrue(
            deadlock_replay_ok("deadlock", "diverged", search_clean=True)
        )
        self.assertFalse(
            deadlock_replay_ok("deadlock", "deadlock", search_clean=True)
        )

    def test_fair_policy_on_the_lock_subject(self) -> None:
        buggy = run_fair(deadlock_d2("buggy"), n_max=2, k=8)
        self.assertEqual(buggy.terminal, "deadlock")
        fixed = run_fair(deadlock_d2("fixed"), n_max=2, k=8)
        self.assertEqual(fixed.terminal, "pass")
        self.assertTrue(all(thread.finished for thread in fixed.threads.values()))


class ArtifactShapeTests(unittest.TestCase):
    def test_rejects_a_bad_schedule(self) -> None:
        payload = json.loads((EXAMPLES / "atomicity_d2_buggy.json").read_text(encoding="utf-8"))
        payload["schedule"] = [1, 2, True]
        with self.assertRaises(ArtifactError):
            validate(payload)
        missing = dict(payload)
        missing["schedule"] = [1, 2, 1, 2]
        del missing["gil"]
        with self.assertRaises(ArtifactError):
            validate(missing)
        negative_pad = dict(payload)
        negative_pad["schedule"] = [1, 2, 1, 2]
        negative_pad["pad"] = -1
        with self.assertRaises(ArtifactError):
            validate(negative_pad)

    def test_checked_in_metadata_matches_a_fresh_replay(self) -> None:
        for name in ("ordering_d1_buggy", "atomicity_d2_buggy", "deadlock_d2_buggy"):
            payload, machine = _replay_fixture(EXAMPLES / f"{name}.json", "buggy")
            self.assertEqual(machine.terminal, payload["terminal"], msg=name)
            self.assertEqual(machine.step, payload["steps"], msg=name)
            self.assertEqual(machine.preemptions, payload["preemptions"], msg=name)
            self.assertEqual(machine.delays, payload["delays"], msg=name)
            self.assertEqual(machine.cells, payload["final_cells"], msg=name)
            self.assertEqual(payload["k"], machine_budget(payload["subject"]), msg=name)


def machine_budget(name: str) -> int:
    return {
        "ordering_d1": ordering_d1,
        "atomicity_d2": atomicity_d2,
        "deadlock_d2": deadlock_d2,
    }[name]("buggy").step_budget()


if __name__ == "__main__":
    unittest.main()
