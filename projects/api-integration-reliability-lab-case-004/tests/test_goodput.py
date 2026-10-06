"""Fixture-scale brownout. Forty-eight clients do not prove a large deployment."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401  inserts the project src path
from lotcycle.harness import ShedGate, simulate_brownout


class GoodputTest(unittest.TestCase):
    def test_steady_state_is_stable_without_the_trigger(self) -> None:
        # The collapse below needs the brownout: the same load is stable without it.
        for arm in ("unbounded", "budgeted"):
            with self.subTest(arm=arm):
                result = simulate_brownout(arm, seed=3, brownout=False)
                self.assertEqual(set(result["goodput"]), {result["clients"]})
                self.assertTrue(all(item < result["capacity"] for item in result["attempts"]))
                self.assertEqual(result["shed_count"], 0)

    def test_unbounded_retries_stay_at_zero_goodput(self) -> None:
        result = simulate_brownout("unbounded", seed=3)
        self.assertEqual(result["baseline_goodput"], result["clients"])
        before = result["attempts"][: result["brownout_start"]]
        self.assertTrue(all(item < result["capacity"] for item in before))
        for tick in range(result["brownout_end"], len(result["goodput"])):
            self.assertEqual(result["goodput"][tick], 0)
            self.assertGreater(result["attempts"][tick], result["capacity"])
        self.assertIn(result["timeout_s"], result["latency"])
        self.assertEqual(result["shed_count"], 0)

    def test_budgeted_arm_recovers_and_sheds(self) -> None:
        result = simulate_brownout("budgeted", seed=3)
        self.assertEqual(result["baseline_goodput"], result["clients"])
        self.assertEqual(result["goodput"][result["brownout_end"] - 1], 0)
        self.assertEqual(result["goodput"][-1], result["baseline_goodput"])
        self.assertTrue(all(item == result["clients"] for item in result["goodput"][-20:]))
        self.assertTrue(all(item <= result["capacity"] for item in result["attempts"][-20:]))
        self.assertIn(result["timeout_s"], result["latency"])
        self.assertGreaterEqual(result["shed_count"], 1)
        self.assertEqual(len(result["shed_logs"]), result["shed_count"])
        self.assertEqual(len(result["sheds"]), result["shed_count"])
        for shed in result["sheds"]:
            self.assertEqual(shed["status"], 503)
            self.assertEqual(shed["retry_after"], "1")
            self.assertEqual(shed["cache-control"], "no-store")
            self.assertEqual(shed["pragma"], "no-cache")
        for record in result["shed_logs"]:
            self.assertEqual(record, {"event": "shed"})

    def test_ablation_attributes_recovery_to_the_shed_gate(self) -> None:
        """Shedding is what restores goodput here; the budget lowers endpoint load."""
        end = simulate_brownout("budgeted", seed=3)["brownout_end"]
        for seed in (1, 3, 7):
            with self.subTest(seed=seed):
                both = simulate_brownout("budgeted", seed=seed)
                no_shed = simulate_brownout("budgeted", seed=seed, shed=False)
                shed_only = simulate_brownout("unbounded", seed=seed, shed=True)
                clients = both["clients"]
                self.assertLess(min(no_shed["goodput"][-30:]), clients)
                self.assertEqual(shed_only["goodput"][-1], clients)
                self.assertLess(sum(both["attempts"][end:]), sum(shed_only["attempts"][end:]))
                self.assertLess(both["shed_count"], shed_only["shed_count"])

    def test_shed_gate_does_not_walk_the_stack(self) -> None:
        names = set(ShedGate.reject.__code__.co_names)
        self.assertTrue(names.isdisjoint({"print", "excepthook", "extract_stack", "format_exc"}))
        gate = ShedGate()
        first = gate.reject()
        self.assertEqual(first["status"], 503)
        self.assertEqual(gate.count, 1)
        self.assertEqual(gate.logs, [{"event": "shed"}])


if __name__ == "__main__":
    unittest.main()
