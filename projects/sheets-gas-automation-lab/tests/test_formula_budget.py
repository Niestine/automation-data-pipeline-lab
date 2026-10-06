"""Local staging-formula gates. Ceilings are policy, not published cutoffs."""

from __future__ import annotations

import unittest
from decimal import Decimal

import support  # noqa: F401

from sessionfee.formula_budget import (
    LOCAL_CHAIN_CEILING,
    LOCAL_MAX_HEIGHT,
    LOCAL_MAX_RANGES,
    evaluate_chain,
    evaluate_formula,
    staging_sum_formula,
)
from sessionfee.dimensions import HOUR, JPY, JPY_PER_HOUR, Quantity
from sessionfee.transform import session_amount


class FormulaBudgetTests(unittest.TestCase):
    def test_sum_one_range_passes_budget(self) -> None:
        metrics = evaluate_formula("=SUM(A2:A10)")
        self.assertTrue(metrics.accepted)
        self.assertEqual(metrics.height, 1)
        self.assertEqual(metrics.range_count, 1)
        self.assertFalse(metrics.conditional)
        self.assertEqual(metrics.reference_count, 9)
        self.assertLessEqual(metrics.height, LOCAL_MAX_HEIGHT)
        self.assertLessEqual(metrics.range_count, LOCAL_MAX_RANGES)

    def test_if_fails_only_the_conditional_gate_when_it_has_one_reference(self) -> None:
        metrics = evaluate_formula("=IF(A2,1,0)")
        self.assertFalse(metrics.accepted)
        self.assertEqual(metrics.failures, ("conditional",))
        self.assertEqual(metrics.height, 1)
        self.assertEqual(metrics.range_count, 1)

    def test_nested_conditional_example_fails_budget(self) -> None:
        metrics = evaluate_formula("=IF(A2>0,B2*C2,0)")
        self.assertFalse(metrics.accepted)
        self.assertIn("conditional", metrics.failures)

    def test_two_ranges_fail_while_reference_count_is_not_the_gate(self) -> None:
        many_cells_one_range = evaluate_formula("=SUM(A2:A100)")
        two_ranges = evaluate_formula("=SUM(A2,B2)")
        self.assertEqual(many_cells_one_range.reference_count, 99)
        self.assertTrue(many_cells_one_range.accepted)
        self.assertEqual(two_ranges.reference_count, 2)
        self.assertFalse(two_ranges.accepted)
        self.assertEqual(two_ranges.failures, ("ranges",))
        self.assertGreater(many_cells_one_range.reference_count, two_ranges.reference_count)

    def test_height_gate_rejects_a_triple_nested_sum(self) -> None:
        metrics = evaluate_formula("=SUM(SUM(SUM(A2)))")
        self.assertFalse(metrics.accepted)
        self.assertEqual(metrics.failures, ("height",))
        self.assertEqual(metrics.height, 3)
        self.assertEqual(metrics.range_count, 1)

    def test_chain_of_eight_passes_per_formula_gate(self) -> None:
        formulas = _linked_sums(8)
        result = evaluate_chain(formulas, LOCAL_CHAIN_CEILING)
        self.assertTrue(result.per_formula_accepted)
        self.assertEqual(result.length, 8)

    def test_local_chain_ceiling_of_4_rejects_eight_formula_chain(self) -> None:
        short = evaluate_chain(_linked_sums(4), LOCAL_CHAIN_CEILING)
        long = evaluate_chain(_linked_sums(8), LOCAL_CHAIN_CEILING)
        self.assertEqual(LOCAL_CHAIN_CEILING, 4)
        self.assertTrue(short.per_formula_accepted)
        self.assertEqual(short.length, 4)
        self.assertTrue(short.ceiling_accepted)
        self.assertTrue(long.per_formula_accepted)
        self.assertEqual(long.length, 8)
        self.assertFalse(long.ceiling_accepted)

    def test_python_rule_is_outside_the_formula_budget(self) -> None:
        # The sheet version of the row rule is rejected by the budget; the
        # same rule in Python computes the amount, and the only formula the
        # planner emits is the one-range staging SUM.
        sheet_rule = evaluate_formula("=IF(D2>0,D2*E2,0)")
        self.assertFalse(sheet_rule.accepted)
        self.assertIn("conditional", sheet_rule.failures)
        amount = session_amount(
            Quantity(Decimal("2.5"), HOUR),
            Quantity(Decimal("4000"), JPY_PER_HOUR),
        )
        self.assertEqual(amount.value, Decimal("10000"))
        self.assertEqual(amount.dimension, JPY)
        emitted = evaluate_formula(staging_sum_formula("F", 2, 4))
        self.assertTrue(emitted.accepted)
        self.assertEqual((emitted.height, emitted.range_count), (1, 1))


def _linked_sums(length: int) -> dict[str, str]:
    formulas: dict[str, str] = {}
    previous = "B2"
    for offset in range(length):
        cell = f"{chr(ord('C') + offset)}2"
        formulas[cell] = f"=SUM({previous})"
        previous = cell
    return formulas


if __name__ == "__main__":
    unittest.main()
