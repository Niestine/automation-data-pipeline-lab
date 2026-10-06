"""Bottom-line oracle versus the per-cell schema baseline."""

from __future__ import annotations

import inspect
import unittest
from decimal import Decimal

import support  # noqa: F401

from sessionfee.dimensions import Quantity
from sessionfee.fixtures import decimal_field, load_json, rows_from_document
from sessionfee.invariants import check_invariant
from sessionfee.schema import check_rows, dimensions_consistent
from sessionfee.transform import dropping_total, first_row_only_total, group_total


class BottomLineOracleTests(unittest.TestCase):
    def test_fixture_a_schema_baseline_passes_and_invariant_fails(self) -> None:
        document = load_json("fixture_a_cell_valid_wrong_total.json")
        rows = rows_from_document(document)
        schema = check_rows(rows)
        hand_sum = Decimal("10000") + Decimal("3500") + Decimal("2000")
        self.assertEqual(decimal_field(document, "expected_total_jpy"), hand_sum)
        self.assertTrue(schema.passed)
        self.assertEqual(schema.errors, ())
        amounts = [row.amount for row in rows if isinstance(row.amount, Quantity)]
        self.assertEqual(first_row_only_total(amounts).value, decimal_field(document, "written_total_jpy"))
        self.assertNotEqual(first_row_only_total(amounts).value, hand_sum)
        invariant = check_invariant(
            written_total=decimal_field(document, "written_total_jpy"),
            expected_total=decimal_field(document, "expected_total_jpy"),
            written_row_count=len(rows),
            expected_row_count=int(document["expected_row_count"]),
        )
        self.assertFalse(invariant.passed)
        self.assertIn("total", invariant.reasons)
        self.assertNotIn("row_count", invariant.reasons)

    def test_fixture_a_passes_dimensions_and_fails_invariant(self) -> None:
        document = load_json("fixture_a_cell_valid_wrong_total.json")
        rows = rows_from_document(document)
        dimension_passes = [dimensions_consistent(row) for row in rows]
        schema = check_rows(rows)
        self.assertEqual(dimension_passes.count(True), len(rows))
        self.assertTrue(schema.passed)
        invariant = check_invariant(
            written_total=decimal_field(document, "written_total_jpy"),
            expected_total=decimal_field(document, "expected_total_jpy"),
            written_row_count=len(rows),
            expected_row_count=int(document["expected_row_count"]),
        )
        self.assertFalse(invariant.passed)

    def test_fixture_b_schema_baseline_already_fails(self) -> None:
        document = load_json("fixture_b_non_numeric_amount.json")
        schema = check_rows(rows_from_document(document))
        self.assertFalse(schema.passed)
        self.assertTrue(any("amount_type" in error for error in schema.errors))

    def test_fixture_c_shared_function_oracle_is_blind(self) -> None:
        document = load_json("fixture_c_shared_oracle.json")
        rows = rows_from_document(document)
        amounts = [row.amount for row in rows if isinstance(row.amount, Quantity)]
        written = dropping_total(amounts)
        shared_oracle = dropping_total(amounts)
        literal = decimal_field(document, "expected_total_jpy")
        self.assertEqual(written.value, shared_oracle.value)
        self.assertNotEqual(written.value, literal)
        self.assertTrue(
            check_invariant(
                written_total=written.value,
                expected_total=shared_oracle.value,
                written_row_count=len(rows),
                expected_row_count=len(rows),
            ).passed
        )
        self.assertFalse(
            check_invariant(
                written_total=written.value,
                expected_total=literal,
                written_row_count=len(rows),
                expected_row_count=int(document["expected_row_count"]),
            ).passed
        )
        self.assertEqual(group_total(amounts).value, literal)

    def test_invariant_checker_does_not_call_the_writer_total(self) -> None:
        source = inspect.getsource(check_invariant)
        self.assertNotIn("group_total", source)
        self.assertNotIn("dropping_total", source)


if __name__ == "__main__":
    unittest.main()
