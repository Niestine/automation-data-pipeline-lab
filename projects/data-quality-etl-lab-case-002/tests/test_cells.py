import unittest

import helpers  # noqa: F401

from edition_gate.cells import parse_cell
from edition_gate.model import column, schema
from edition_gate.pipeline import ingest_bytes
from edition_gate.registry import Registry


def _load(columns, payload, **kwargs):
    registry = Registry()
    result = registry.register(schema(1, columns), [])
    if not result["ok"]:
        raise AssertionError(result["reasons"])
    return ingest_bytes(registry, payload, writer_version=1, link=False, **kwargs)


class CellTests(unittest.TestCase):
    def test_decimal_null_token_and_type_error_continue(self):
        columns = [
            column("sku", required=True),
            column(
                "price",
                datatype="decimal",
                decimal_char=",",
                group_char=".",
                null_tokens=("NA",),
            ),
        ]
        payload = 'sku,price\nA," 12,50 "\nB,NA\nC,one\nD,"1.234,50"\n'.encode("utf-8")
        result = _load(columns, payload)
        rows = {row["cells"]["sku"]["value"]: row["cells"]["price"] for row in result["rows"]}
        self.assertEqual(rows["A"]["string_value"], " 12,50 ")
        self.assertEqual(rows["A"]["value"], "12.50")
        self.assertEqual(rows["A"]["errors"], [])
        self.assertIsNone(rows["B"]["value"])
        self.assertNotIn("type_error", rows["B"]["errors"])
        self.assertEqual(rows["C"]["value"], "one")
        self.assertIn("type_error", rows["C"]["errors"])
        self.assertEqual(rows["D"]["value"], "1234.50")
        self.assertEqual(result["status"], "accepted_with_errors")

    def test_integer_failure_does_not_drop_the_next_row(self):
        result = _load(
            [column("n", datatype="integer", required=True)],
            b"n\none\n2\n",
        )
        self.assertEqual(result["rows"][0]["cells"]["n"]["value"], "one")
        self.assertIn("type_error", result["rows"][0]["cells"]["n"]["errors"])
        self.assertEqual(result["rows"][1]["cells"]["n"]["value"], 2)

    def test_required_null_stays_on_the_row(self):
        result = _load(
            [column("n", datatype="integer", required=True, null_tokens=("NA",))],
            b"n\nNA\n",
        )
        cell = result["rows"][0]["cells"]["n"]
        self.assertIsNone(cell["value"])
        self.assertEqual(cell["errors"], ["required_null"])
        self.assertEqual(result["status"], "accepted_with_errors")

    def test_date_pattern_and_percent(self):
        dated = parse_cell("10/18/2010", column("when", datatype="date", format="M/d/yyyy", null_tokens=()))
        self.assertEqual(dated["value"], "2010-10-18")
        stamped = parse_cell(
            "2010-10-18T14:30:00",
            column("when", datatype="datetime", format="yyyy-MM-ddTHH:mm:ss", null_tokens=()),
        )
        self.assertEqual(stamped["value"], "2010-10-18T14:30:00")
        percent = parse_cell("50%", column("rate", datatype="decimal", null_tokens=()))
        self.assertEqual(percent["value"], "0.50")
        scaled = parse_cell("1.5e2", column("n", datatype="decimal", null_tokens=()))
        self.assertEqual(scaled["value"], "150")

    def test_present_empty_is_not_replaced_by_the_default(self):
        # The omitted-column half of this rule is in
        # test_pipeline.test_old_writer_may_omit_a_new_column_and_the_current_writer_may_not.
        columns = [
            column("name", required=True, null_tokens=()),
            column("color", has_default=True, default="green", null_tokens=()),
        ]
        present = _load(columns, b'name,color\nAcme,""\nBlue,blue\n')
        self.assertEqual(present["rows"][0]["cells"]["color"]["string_value"], "")
        self.assertEqual(present["rows"][0]["cells"]["color"]["value"], "")
        self.assertEqual(present["rows"][0]["cells"]["color"]["syntactic_empty"], "quoted")
        self.assertNotIn("omitted", present["rows"][0]["cells"]["color"])
        self.assertEqual(present["rows"][1]["cells"]["color"]["value"], "blue")

    def test_max_length_and_non_finite_double_are_cell_errors(self):
        long_code = parse_cell("BIN-12345", column("bin_code", max_length=5, null_tokens=()))
        self.assertEqual(long_code["value"], "BIN-12345")
        self.assertEqual(long_code["errors"], ["length_error"])
        short_code = parse_cell("BIN-1", column("bin_code", max_length=5, null_tokens=()))
        self.assertEqual(short_code["errors"], [])
        huge = parse_cell("1e400", column("weight", datatype="double", null_tokens=()))
        self.assertEqual(huge["value"], "1e400")
        self.assertEqual(huge["errors"], ["type_error"])
        grouped = parse_cell("1,000", column("qty", datatype="integer", null_tokens=()))
        self.assertEqual(grouped["errors"], ["type_error"])

    def test_error_budget_quarantines_without_a_partial_table(self):
        columns = [column("n", datatype="integer")]
        blocked = _load(columns, b"n\none\n2\n", error_budget=0)
        self.assertEqual(blocked["reason"], "error_budget")
        self.assertEqual(blocked["rows"], [])


if __name__ == "__main__":
    unittest.main()
