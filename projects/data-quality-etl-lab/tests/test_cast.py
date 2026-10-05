"""Physical missing values, logical casts, and NFC versus NFKC."""

from __future__ import annotations

import json
import re
import unicodedata
import unittest
from decimal import Decimal

import support
from harbor_ledger.csvio import parse_csv
from harbor_ledger.errors import SchemaError
from harbor_ledger.pipeline import run_bytes
from harbor_ledger.schema import cast_table, load_schema, schema_from_dict, standardize_text


def _schema(fields, **extra):
    payload = {
        "name": "cast-lab",
        "primaryKey": [fields[0]["name"]],
        "missingValues": extra.get("missingValues", [""]),
        "dialect": extra.get("dialect", {"header": True, "trim": False}),
        "fields": fields,
        "rules": extra.get("rules", []),
    }
    return schema_from_dict(payload)


def _cast(text, schema):
    parsed = parse_csv(text, schema.dialect, len(schema.fields))
    return cast_table(parsed.records, schema)


class CastTests(unittest.TestCase):
    def test_na_is_null_only_when_listed(self) -> None:
        listed = _schema(
            [{"name": "amount", "type": "number"}],
            missingValues=["", "NA"],
        )
        unlisted = _schema([{"name": "amount", "type": "number"}], missingValues=[""])
        self.assertIsNone(_cast("amount\nNA\n", listed)[0].values["amount"])
        self.assertEqual(_cast("amount\nNA\n", listed)[0].cast_errors, {})
        self.assertTrue(_cast("amount\nNA\n", unlisted)[0].cast_errors["amount"])
        dash = _schema(
            [{"name": "amount", "type": "number"}],
            missingValues=["", "-"],
        )
        self.assertIsNone(_cast("amount\n-\n", dash)[0].values["amount"])

    def test_group_char_and_listed_percent_affix(self) -> None:
        grouped = _schema(
            [{"name": "amount", "type": "number", "groupChar": ",", "decimalChar": "."}]
        )
        plain = _schema([{"name": "amount", "type": "number"}])
        quoted = 'amount\n"1,234.5"\n'
        self.assertEqual(_cast(quoted, grouped)[0].values["amount"], Decimal("1234.5"))
        self.assertTrue(_cast(quoted, plain)[0].cast_errors["amount"])
        percent = _schema(
            [
                {
                    "name": "rate",
                    "type": "number",
                    "bareNumber": False,
                    "affixes": {"suffixes": ["%"]},
                }
            ]
        )
        row = _cast("rate\n95%\n", percent)[0]
        self.assertEqual(row.values["rate"], Decimal("95"))
        self.assertIn("stripped %", row.notes["rate"])
        bare = _schema([{"name": "rate", "type": "number", "bareNumber": True}])
        self.assertTrue(_cast("rate\n95%\n", bare)[0].cast_errors["rate"])

    def test_dates_do_not_guess_and_booleans_use_token_lists(self) -> None:
        iso = _schema([{"name": "day", "type": "date", "format": "YYYY-MM-DD"}])
        regional = _schema([{"name": "day", "type": "date", "format": "%d/%m/%y"}])
        first = _cast("day\n01/02/2020\n", iso)[0]
        second = _cast("day\n01/02/2020\n", iso)[0]
        self.assertIsNone(first.values["day"])
        self.assertTrue(first.cast_errors["day"])
        self.assertEqual(first.cast_errors, second.cast_errors)
        self.assertEqual(_cast("day\n30/11/14\n", regional)[0].values["day"].isoformat(), "2014-11-30")
        flagged = _schema([{"name": "active", "type": "boolean"}])
        self.assertTrue(_cast("active\nyes\n", flagged)[0].cast_errors["active"])
        self.assertIs(_cast("active\nTRUE\n", flagged)[0].values["active"], True)

    def test_pattern_is_a_full_string_match(self) -> None:
        schema = _schema(
            [{"name": "code", "type": "string", "constraints": {"pattern": "[0-9]+"}}]
        )
        self.assertIsNotNone(re.search(r"[0-9]+", "abc123"))
        self.assertTrue(_cast("code\nabc123\n", schema)[0].cast_errors == {})
        from harbor_ledger.schema import pattern_error

        self.assertIsNotNone(pattern_error("abc123", schema.fields[0]))
        self.assertIsNone(pattern_error("123", schema.fields[0]))

    def test_primary_key_null_is_required(self) -> None:
        schema = _schema(
            [
                {"name": "listing_id", "type": "string"},
                {"name": "title", "type": "string"},
            ],
            missingValues=[""],
        )
        row = _cast("listing_id,title\n,Coat\n", schema)[0]
        self.assertIsNone(row.values["listing_id"])
        self.assertEqual(row.row_id, "")

    def test_format_any_is_rejected(self) -> None:
        with self.assertRaises(SchemaError):
            _schema([{"name": "day", "type": "date", "format": "any"}])

    def test_malformed_dialect_is_a_schema_error(self) -> None:
        field = [{"name": "code", "type": "string"}]
        for dialect in (
            {"skip_rows": "two"},
            {"trim": "yes"},
            {"delimiter": ";;"},
            {"delimiter": '"'},
        ):
            with self.subTest(dialect=dialect), self.assertRaises(SchemaError):
                _schema(field, dialect=dialect)

    def test_normalization_and_abbreviations(self) -> None:
        decomposed = "e\u0301"
        composed = "\u00e9"
        self.assertEqual(unicodedata.normalize("NFC", decomposed), unicodedata.normalize("NFC", composed))
        once = unicodedata.normalize("NFC", decomposed)
        self.assertEqual(unicodedata.normalize("NFC", once), once)
        self.assertEqual(unicodedata.normalize("NFC", "\u2460"), "\u2460")
        self.assertEqual(unicodedata.normalize("NFKC", "\u2460"), "1")
        self.assertEqual(unicodedata.normalize("NFC", "\ufb01"), "\ufb01")
        self.assertEqual(unicodedata.normalize("NFKC", "\ufb01"), "fi")
        self.assertEqual(
            standardize_text("44 W. 4th St."),
            standardize_text("44 West Fourth Street"),
        )
        self.assertEqual(standardize_text("Café Coat"), "café coat")
        self.assertEqual(standardize_text("Café"), "café")
        self.assertEqual(standardize_text("紺のコート"), "紺のコート")

    def test_identifier_fold_is_logged_and_description_is_not(self) -> None:
        schema = load_schema(support.EXAMPLES / "schema.json")
        profile = json.loads((support.EXAMPLES / "profile.json").read_text(encoding="utf-8"))
        text = (
            "listing_id,supplier_code,style_code,department,title,color,wholesale_usd,listed_on\n"
            "HL100,NORTH,ＫＴ３１０,knit,fi\ufb01eld note,navy,20.00,2026-03-01\n"
        )
        result = run_bytes(text.encode("utf-8"), schema, profile)
        folds = [item for item in result.report["annotations"] if item["kind"] == "nfkc"]
        self.assertEqual(len(folds), 1)
        self.assertEqual(folds[0]["column"], "style_code")
        self.assertEqual(folds[0]["before"], "ＫＴ３１０")
        self.assertEqual(folds[0]["after"], "KT310")
        self.assertEqual(result.report["clean_rows"][0]["title"], "fi\ufb01eld note")
        self.assertEqual(result.report["clean_rows"][0]["style_code"], "ＫＴ３１０")


if __name__ == "__main__":
    unittest.main()
