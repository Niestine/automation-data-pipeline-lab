"""PCT hit rates, preemption bounds, replay, and budget refusals."""

from __future__ import annotations

import unittest

from support import REGRESSIONS, execute
from yieldlab.campaign import pct_hits
from yieldlab.engine import Schedule, run, search_first
from yieldlab.errors import BoundExceeded, Deadlock, Invariant, LabError, ReplayDivergence
from yieldlab.journal import hash_events, load_journal
from yieldlab.scenarios import build


class SchedulerTests(unittest.TestCase):
    def test_depth1_hit_rate_clears_half_bound(self) -> None:
        case = build("ordering_d1", "unfixed")
        sample = 20 * 4
        hits = pct_hits(
            case, sample=sample, mode="free", depth=1, k_budget=4, n_max=4
        )
        rate = hits / sample
        floor = 1 / (2 * 4)
        self.assertGreaterEqual(
            rate,
            floor,
            f"hits={hits} sample={sample} rate={rate:.3f} floor={floor:.3f}",
        )

    def test_depth2_hit_rate_clears_half_bound(self) -> None:
        case = build("lost_update", "unfixed")
        n = 2
        k = 6
        sample = 20 * n * k
        hits = pct_hits(
            case, sample=sample, mode="free", depth=2, k_budget=k, n_max=n
        )
        rate = hits / sample
        floor = 1 / (2 * n * k)
        self.assertGreaterEqual(
            rate,
            floor,
            f"hits={hits} sample={sample} rate={rate:.3f} floor={floor:.3f} k={k}",
        )

    def test_fixed_ordering_is_quiet(self) -> None:
        case = build("ordering_d1", "fixed")
        for depth in (1, 2):
            hits = pct_hits(
                case, sample=40, mode="free", depth=depth, k_budget=8, n_max=4
            )
            self.assertEqual(hits, 0, f"depth={depth}")
        trace = execute(case, seed=1, depth=1, n_max=4, k_budget=8)
        self.assertEqual(trace.races, [])
        self.assertEqual(trace.cells["observed"], 1)

    def test_ordering_replay_is_stable(self) -> None:
        journal = load_journal(REGRESSIONS / "ordering_d1_unfixed.schedule.json")
        self.assertEqual(journal["trace_sha256"], hash_events(journal["events"]))
        hashes = []
        for _ in range(20):
            case = build("ordering_d1", "unfixed")
            schedule = Schedule(
                mode="free",
                policy="replay",
                replay_events=journal["events"],
                k_budget=8,
                n_max=4,
            )
            with self.assertRaises(Invariant) as caught:
                run(case, schedule)
            self.assertEqual(caught.exception.journal["trace_sha256"], journal["trace_sha256"])
            hashes.append(caught.exception.journal["trace_sha256"])
        self.assertEqual(len(set(hashes)), 1)

    def test_same_seed_same_trace(self) -> None:
        case = build("lost_update", "locked")
        first = execute(case, seed=3, depth=2, n_max=2, k_budget=16, mode="free")
        second = execute(case, seed=3, depth=2, n_max=2, k_budget=16, mode="free")
        self.assertEqual(first.events, second.events)
        self.assertEqual(first.sha256, second.sha256)

    def test_cancel_bound0_misses_and_bound2_hits(self) -> None:
        case = build("cancel_port", "unfixed")
        schedule = Schedule(mode="free", seed=0, depth=1, n_max=8, k_budget=32)
        self.assertIsNone(search_first(case, schedule, max_bound=0))
        self.assertIsNone(search_first(case, schedule, max_bound=1))
        hit = search_first(case, schedule, max_bound=2)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.bound, 2)
        self.assertEqual(hit.journal["failure"], "Deadlock")
        self.assertEqual(schedule.preemption_bound, 0)
        saved = load_journal(REGRESSIONS / "cancel_unfixed.schedule.json")
        # The search is deterministic: a fresh walk rebuilds the saved journal.
        self.assertEqual(hit.journal, saved)
        self.assertEqual(saved["preemption_bound"], 2)
        self._replay_cancel(saved["events"], "unfixed", Deadlock)
        self._replay_cancel(saved["events"], "fixed", None)
        self._replay_cancel(saved["events"], "fixed", None, mode="gil")

    def _replay_cancel(self, events, variant: str, expected, mode: str = "free") -> None:
        case = build("cancel_port", variant)
        schedule = Schedule(
            mode=mode,
            policy="replay",
            replay_events=events,
            switch_interval=1,
            k_budget=32,
            n_max=8,
        )
        if expected is None:
            trace = run(case, schedule)
            port = trace.extra["port"]
            self.assertFalse(port["stuck"])
            self.assertTrue(port["flag"])
            # The journal prefix ran, then the worker finished the port.
            self.assertEqual(trace.events[: len(events)], events)
            self.assertIn("confirmed", trace.marks)
            self.assertEqual(port["handled"], [("cancel", "scan-a"), ("cancel", "scan-b")])
            return
        with self.assertRaises(expected):
            run(case, schedule)

    def test_cancel_fixed_search_is_clean(self) -> None:
        case = build("cancel_port", "fixed")
        schedule = Schedule(mode="free", seed=0, depth=1, n_max=8, k_budget=32)
        self.assertIsNone(search_first(case, schedule, max_bound=2))

    def test_replay_divergence(self) -> None:
        case = build("ordering_d1", "unfixed")
        schedule = Schedule(
            mode="free",
            policy="replay",
            replay_events=[{"i": 0, "thread": 9, "op": "bc", "args": []}],
            k_budget=8,
            n_max=4,
        )
        with self.assertRaises(ReplayDivergence):
            run(case, schedule)

    def test_n_max_and_k_budget_refuse_the_run(self) -> None:
        wide = build("ordering_d1", "unfixed")
        with self.assertRaises(BoundExceeded):
            execute(wide, n_max=2, k_budget=8, depth=1)
        short = build("lost_update", "unfixed")
        with self.assertRaises(BoundExceeded):
            execute(short, n_max=2, k_budget=2, depth=1, seed=0)
        # Six steps fit a budget of six exactly; the serial run is correct.
        trace = execute(short, n_max=2, k_budget=6, depth=1, seed=0)
        self.assertEqual(len(trace.events), 6)

    def test_campaign_refuses_runs_outside_the_bound(self) -> None:
        # A run past k_budget is a setup error. Counting it as a hit would
        # report a 100% rate for a scenario that never reached its window.
        case = build("lost_update", "unfixed")
        with self.assertRaises(BoundExceeded):
            pct_hits(case, sample=10, mode="free", depth=1, k_budget=4, n_max=4)
        with self.assertRaises(ValueError):
            pct_hits(case, sample=0, mode="free", depth=1, k_budget=6, n_max=2)

    def test_locked_update_passes_gil_and_free(self) -> None:
        case = build("lost_update", "locked")
        for mode in ("gil", "free"):
            trace = execute(
                case, mode=mode, seed=5, depth=2, n_max=2, k_budget=16, switch_interval=1
            )
            self.assertEqual(trace.cells["count"], 2)
            self.assertEqual(trace.races, [])
            hits = pct_hits(
                case, sample=240, mode=mode, depth=2, k_budget=10, n_max=2, switch_interval=1
            )
            self.assertEqual(hits, 0, mode)

    def test_unfixed_update_fails_when_the_quantum_is_one_step(self) -> None:
        case = build("lost_update", "unfixed")
        found = None
        for seed in range(30):
            free_fail = _fails(case, mode="free", seed=seed, switch_interval=1)
            gil_fail = _fails(case, mode="gil", seed=seed, switch_interval=1)
            if free_fail and gil_fail:
                found = seed
                break
        self.assertIsNotNone(found)
        assert found is not None
        self.assertFalse(_fails(case, mode="gil", seed=found, switch_interval=100))
        locked = build("lost_update", "locked")
        self.assertFalse(_fails(locked, mode="free", seed=found, switch_interval=1))
        self.assertFalse(_fails(locked, mode="gil", seed=found, switch_interval=1))


def _fails(case, **kwargs) -> bool:
    # The locked body is five steps per thread; the unfixed body is three.
    k_budget = 10 if case.variant == "locked" else 6
    try:
        execute(case, depth=2, n_max=2, k_budget=k_budget, **kwargs)
    except BoundExceeded:
        raise
    except LabError:
        return True
    return False


if __name__ == "__main__":
    unittest.main()
