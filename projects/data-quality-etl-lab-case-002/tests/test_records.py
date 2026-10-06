import unittest

import helpers  # noqa: F401

from edition_gate.model import column, dialect, schema
from edition_gate.pipeline import ingest_bytes
from edition_gate.records import parse_text
from edition_gate.registry import Registry


def _registry(columns, **dialect_kwargs):
    registry = Registry()
    built = schema(1, columns, dialect=dialect(**dialect_kwargs))
    result = registry.register(built, [])
    if not result["ok"]:
        raise AssertionError(result["reasons"])
    return registry


class RecordTests(unittest.TestCase):
    def test_spaces_quotes_and_trailing_delimiter(self):
        parsed = parse_text('name\n abc\nabc \n1,"2,5"\n', ",", '"', "", "lf")
        self.assertEqual(parsed[1]["fields"], [" abc"])
        self.assertEqual(parsed[2]["fields"], ["abc "])
        self.assertEqual(parsed[3]["fields"], ["1", "2,5"])

    def test_syntactic_empty_and_doubled_quote(self):
        parsed = parse_text('a,b,c\n1,"",3\n4,,6\n7,"a""b",8\n', ",", '"', "", "lf")
        self.assertEqual(parsed[1]["kinds"][1], "quoted")
        self.assertEqual(parsed[1]["fields"][1], "")
        self.assertEqual(parsed[2]["kinds"][1], "omitted")
        self.assertEqual(parsed[2]["fields"][1], "")
        self.assertEqual(parsed[3]["fields"][1], 'a"b')

    def test_embedded_break_and_bad_quote(self):
        parsed = parse_text('a,b\n1,"x\ny"\n2,z\n3,4"q\n', ",", '"', "", "lf")
        self.assertEqual(parsed[1]["fields"], ["1", "x\ny"])
        self.assertEqual(parsed[1]["source_row"], 2)
        self.assertEqual(parsed[2]["fields"], ["2", "z"])
        self.assertEqual(parsed[2]["source_row"], 4)
        self.assertEqual(parsed[3]["reason"], "bad_quote")
        after_close = parse_text('a,b\n1,"ab"c\n2,"ok"\n', ",", '"', "", "lf")
        self.assertEqual(after_close[1]["reason"], "bad_quote")
        self.assertIsNone(after_close[2]["reason"])
        self.assertEqual(after_close[2]["fields"], ["2", "ok"])

    def test_header_absent_does_not_eat_the_first_row(self):
        registry = _registry(
            [column("supplier_id", required=True), column("sku"), column("org_name", required=True)],
            header="absent",
        )
        result = ingest_bytes(
            registry,
            b"SUP1,HX-30,Northwind\nSUP2,HX-40,Fabrikam\n",
            writer_version=1,
            link=False,
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["headers"][0]["titles"], ["column_1"])
        self.assertEqual(result["rows"][0]["source_row"], 1)
        self.assertEqual(result["rows"][0]["cells"]["supplier_id"]["value"], "SUP1")
        self.assertEqual(result["rows"][1]["cells"]["org_name"]["value"], "Fabrikam")

    def test_skip_and_two_header_rows(self):
        columns = [
            column("supplier_id", required=True),
            column("organization", aliases=("org_name",), titles=(("Organization", "en"),), required=True),
            column("title", titles=(("Title", "en"), ("name", "en")), required=True),
        ]
        registry = _registry(columns, skip_rows=1, header_row_count=2)
        payload = (
            "weekly export\n"
            "Supplier,Organization,Title\n"
            "supplier_id,org_name,name\n"
            "SUP1,Acme,Bolt\n"
        ).encode("utf-8")
        result = ingest_bytes(registry, payload, writer_version=1, link=False)
        self.assertEqual(result["status"], "accepted", result["reason"])
        self.assertEqual(result["headers"][0]["titles"], ["Supplier", "supplier_id"])
        self.assertEqual(result["headers"][1]["column"], "organization")
        self.assertEqual(result["rows"][0]["source_row"], 4)
        self.assertEqual(result["rows"][0]["row_number"], 1)
        self.assertEqual(result["rows"][0]["cells"]["title"]["value"], "Bolt")

    def test_two_headers_on_one_column_refuse_the_file(self):
        registry = _registry(
            [column("organization", aliases=("org_name",), required=True), column("sku")]
        )
        result = ingest_bytes(
            registry,
            b"organization,sku,org_name\nNorthwind,A,Contoso\n",
            writer_version=1,
            link=False,
        )
        self.assertEqual(result["status"], "quarantine")
        self.assertEqual(result["reason"], "schema_resolution")
        self.assertEqual(result["duplicate_columns"], ["organization"])
        self.assertEqual(result["rows"], [])

    def test_ragged_and_trailing_rows_do_not_stop_the_file(self):
        registry = _registry([column("a", required=True), column("b", required=True)])
        payload = b"a,b\n1,2,\n3,4\n5\n6,7\n"
        result = ingest_bytes(registry, payload, writer_version=1, link=False)
        self.assertEqual([row["cells"]["a"]["value"] for row in result["rows"]], ["3", "6"])
        reasons = [item["reasons"] for item in result["quarantined_rows"]]
        self.assertIn(["trailing_delimiter"], reasons)
        self.assertIn(["ragged_row"], reasons)


if __name__ == "__main__":
    unittest.main()
