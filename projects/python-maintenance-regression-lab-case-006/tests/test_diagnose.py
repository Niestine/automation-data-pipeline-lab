"""Mojibake badness is a counter. Emit does not call it and does not rewrite."""

from __future__ import annotations

import ast
import unittest

import helpers  # noqa: F401
from helpers import LIBRARY, LabTest

from netunicode_lab import assert_interchange_bytes, diagnose_mojibake, emit_interchange_csv


class DiagnoseTests(LabTest):
    def test_accented_letter_plus_currency_counts_one(self):
        self.assertEqual(diagnose_mojibake("\u00e9$"), 1)
        self.assertEqual(diagnose_mojibake("\u00e0\u20ac"), 1)
        self.assertEqual(diagnose_mojibake("caf\u00e9"), 0)
        self.assertEqual(diagnose_mojibake("a$"), 0)
        self.assertEqual(diagnose_mojibake("\u03a9$"), 0)
        self.assertEqual(diagnose_mojibake("\u03c3$"), 0)

    def test_cp1252_reading_of_e_acute_is_flagged(self):
        harness = b"\xc3\xa9".decode("cp1252")
        self.assertEqual(harness, "\u00c3\u00a9")
        self.assertGreater(diagnose_mojibake(harness), 0)

    def test_lossy_markers_inside_a_mojibake_span_count(self):
        self.assertEqual(diagnose_mojibake("\u00c3?"), 1)
        self.assertEqual(diagnose_mojibake("\u00c3\ufffd"), 1)
        self.assertEqual(diagnose_mojibake("why?"), 0)
        self.assertEqual(diagnose_mojibake(""), 0)
        self.assertEqual(diagnose_mojibake("\u00e9"), 0)

    def test_bytes_are_refused(self):
        with self.assertRaises(TypeError):
            diagnose_mojibake(b"\xc3\xa9")

    def test_long_prefix_is_not_the_failure_condition(self):
        prefix = "maintenance log line\n" * 400
        self.assertEqual(diagnose_mojibake(prefix), 0)
        self.assertEqual(diagnose_mojibake(prefix + "\u00e9$"), 1)

    def test_emit_preserves_a_legal_string_the_counter_flags(self):
        field = "\u00e9$"
        self.assertGreater(diagnose_mojibake(field), 0)
        destination = self.root / "flag.csv"
        emit_interchange_csv(destination, [[field]])
        data = destination.read_bytes()
        assert_interchange_bytes(data)
        self.assertIn(field.encode("utf-8"), data)
        self.assertNotIn(b"\xef\xbb\xbf", data)

    def test_diagnose_source_does_not_rewrite(self):
        source = (LIBRARY / "diagnose.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        self.assertNotIn("decode_inconsistent_utf8", names)
        self.assertNotIn("encode", names)

    def test_emit_source_does_not_name_the_counter(self):
        source = (LIBRARY / "emit.py").read_text(encoding="utf-8")
        self.assertNotIn("diagnose_mojibake", source)
        self.assertNotIn("legacy", source)


if __name__ == "__main__":
    unittest.main()
