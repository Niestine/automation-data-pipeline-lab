"""PCT priority mechanics, bounds, fairness, and the step interpreter."""

from __future__ import annotations

import helpers  # noqa: F401
import unittest

from schedlab.campaign import minimum_runs, per_run_lower_bound
from schedlab.errors import MachineError
from schedlab.machine import Machine, Op
from schedlab.oracles import bound_coverage
from schedlab.policies import note_choice, run_fair, run_pct, search_schedules
from schedlab.subjects import Subject, spin_subject


def _always_ok(_machine: object) -> bool:
    return True


class PriorityTests(unittest.TestCase):
    def test_change_points_and_repeated_tid(self) -> None:
        subject = spin_subject(3, 10)
        trace = run_pct(subject, seed=1, n_max=3, k=10, d=3)
        points = trace.change_points
        self.assertEqual(len(points), 2)
        self.assertEqual(len(set(points)), 2)
        self.assertTrue(all(1 <= point <= 10 for point in points))
        self.assertEqual(len(set(trace.initial_priorities.values())), 3)
        schedule = trace.machine.schedule
        self.assertEqual(len(schedule), 10)
        self.assertEqual(len(trace.priorities_after), 10)
        self.assertEqual(trace.machine.max_reentrancy, 1)
        change = set(points)
        index = 0
        while index < len(schedule):
            tid = schedule[index]
            cursor = index
            while cursor < len(schedule) and (cursor + 1) not in change:
                self.assertEqual(schedule[cursor], tid)
                cursor += 1
            if cursor < len(schedule) and (cursor + 1) in change:
                self.assertEqual(schedule[cursor], tid)
                keys = trace.priorities_after[cursor]
                own = keys[schedule[cursor]]
                others = [value for other, value in keys.items() if other != schedule[cursor]]
                self.assertLess(own, min(others))
                if cursor + 1 < len(schedule):
                    self.assertNotEqual(schedule[cursor + 1], schedule[cursor])
                index = cursor + 1
            else:
                while index < len(schedule):
                    self.assertEqual(schedule[index], tid)
                    index += 1

    def test_depth_bound_is_not_shared_across_depths(self) -> None:
        self.assertEqual(per_run_lower_bound(2, 4, 1), 1 / 2)
        self.assertEqual(per_run_lower_bound(2, 4, 2), 1 / (2 * 4))
        self.assertEqual(per_run_lower_bound(3, 10, 3), 1 / (3 * (10 ** 2)))
        self.assertGreater(per_run_lower_bound(2, 4, 1), per_run_lower_bound(2, 4, 3))
        probability = per_run_lower_bound(2, 4, 2)
        runs = minimum_runs(2, 4, 2, delta=0.01)
        self.assertLessEqual((1 - probability) ** runs, 0.01)
        self.assertGreater((1 - probability) ** (runs - 1), 0.01)
        deep = minimum_runs(3, 10, 3, delta=0.01)
        shallow = minimum_runs(3, 10, 1, delta=0.01)
        self.assertGreater(deep, shallow)


class BoundAndFairTests(unittest.TestCase):
    def test_delay_bound_zero_is_the_lowest_tid(self) -> None:
        subject = spin_subject(2, 3)
        outcome = search_schedules(
            subject,
            mode="delay",
            bound=0,
            n_max=2,
            k=6,
            schedule_cap=20,
            stop_on_failure=False,
        )
        self.assertEqual(outcome.schedules_used, 1)
        self.assertEqual(outcome.completed[0].schedule, [1, 1, 1, 2, 2, 2])
        self.assertEqual(outcome.completed[0].delays, 0)
        self.assertFalse(outcome.hit_cap)
        self.assertEqual(outcome.coverage, bound_coverage(0))
        self.assertNotIn("bug free", outcome.coverage.lower())

    def test_fair_finishes_both_threads(self) -> None:
        machine = run_fair(spin_subject(2, 5), n_max=2, k=10)
        self.assertEqual(machine.schedule, [1, 2, 1, 2, 1, 2, 1, 2, 1, 2])
        self.assertEqual(machine.terminal, "pass")
        self.assertTrue(all(thread.finished for thread in machine.threads.values()))
        self.assertEqual(machine.max_reentrancy, 1)

    def test_unfair_pct_step_cap_is_not_a_deadlock(self) -> None:
        trace = run_pct(spin_subject(2, 5), seed=4, n_max=2, k=3, d=1)
        self.assertEqual(trace.machine.terminal, "step-cap")
        self.assertNotEqual(trace.machine.terminal, "deadlock")
        self.assertTrue(trace.machine.enabled_tids())
        self.assertFalse(trace.machine.blocked_tids())


class InterpreterTests(unittest.TestCase):
    def test_step_counter_ignores_plain_list_updates(self) -> None:
        machine = Machine.from_subject(spin_subject(1, 3), n_max=2, k=3)
        bucket: list[int] = []
        self.assertEqual(machine.step, 0)
        bucket.append(1)
        self.assertEqual(machine.step, 0)
        machine.execute(1)
        self.assertEqual(machine.step, 1)
        bucket.append(2)
        self.assertEqual(machine.step, 1)
        self.assertEqual(machine.max_reentrancy, 1)

    def test_reentrancy_guard(self) -> None:
        machine = Machine.from_subject(spin_subject(1, 2), n_max=2, k=2)
        machine._depth = 1
        with self.assertRaises(MachineError):
            machine.execute(1)
        self.assertEqual(machine.step, 0)

    def test_step_past_k_is_bound_exceeded(self) -> None:
        machine = Machine.from_subject(spin_subject(1, 5), n_max=2, k=1)
        machine.execute(1)
        self.assertEqual(machine.terminal, "step-cap")
        self.assertEqual(machine.step, 1)
        machine.terminal = None
        machine.execute(1)
        self.assertEqual(machine.terminal, "bound_exceeded")
        self.assertEqual(machine.step, 1)

    def test_too_many_threads_is_bound_exceeded(self) -> None:
        machine = Machine.from_subject(spin_subject(4, 1), n_max=3, k=4)
        self.assertEqual(machine.terminal, "bound_exceeded")
        self.assertEqual(machine.schedule, [])
        self.assertEqual(machine.step, 0)

    def test_release_wakes_the_lowest_tid(self) -> None:
        subject = Subject(
            name="handoff",
            revision="fixed",
            ops={
                1: (Op("acquire", name="a"), Op("release", name="a")),
                2: (
                    Op("acquire", name="a"),
                    Op("write", name="x", value=2),
                    Op("release", name="a"),
                ),
                3: (
                    Op("acquire", name="a"),
                    Op("write", name="x", value=3),
                    Op("release", name="a"),
                ),
            },
            cells={"x": 0},
            locks=("a",),
            predicate=_always_ok,
        )
        machine = Machine.from_subject(subject, n_max=3, k=8)
        prev = None
        for tid in (1, 2, 3, 1, 2):
            self.assertTrue(note_choice(machine, tid, prev, "replay"))
            prev = tid
            machine.execute(tid)
        self.assertIsNone(machine.terminal)
        self.assertEqual(machine.cells["x"], 2)
        self.assertEqual(machine.locks["a"], 2)
        self.assertIsNone(machine.threads[2].blocked_on)
        self.assertEqual(machine.threads[3].blocked_on, "a")
        self.assertEqual(machine.max_reentrancy, 1)


if __name__ == "__main__":
    unittest.main()
