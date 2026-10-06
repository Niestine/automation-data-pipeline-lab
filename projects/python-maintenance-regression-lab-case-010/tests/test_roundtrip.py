"""The unparser is the only writer, and strict output parses again."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from wharf_sheet.model import UnparseError
from wharf_sheet.recognize import hardened_parse
from wharf_sheet.unparse import unparse, write_sheet


class RoundTripTests(unittest.TestCase):
    def test_special_characters_round_trip_without_backslash_escapes(self):
        records = (("a,b", 'say "hi"', "line\r\nnext", "  spaced  ", "12345678901234567"),)
        emitted = unparse(None, records)
        self.assertFalse(emitted.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\\", emitted)
        self.assertIn(b"\r\n", emitted)
        again = hardened_parse(emitted, header="absent")
        self.assertEqual(again.records, records)
        self.assertEqual(again.events, ())
        self.assertEqual(again.records[0][-1], "12345678901234567")
        self.assertIsInstance(again.records[0][-1], str)
        self.assertEqual(unparse(None, records), emitted)

    def test_none_is_not_written_as_an_empty_field(self):
        with self.assertRaises(UnparseError) as caught:
            unparse(None, [("a", None)])
        self.assertEqual(str(caught.exception), "E-type")

    def test_newline_disabled_write_keeps_a_quoted_line_feed(self):
        records = (("berth",), ("N-1\nS-2",))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sheet.csv"
            write_sheet(path, ("name",), records[1:])
            data = path.read_bytes()
        self.assertNotIn(b"\r\r\n", data)
        self.assertIn(b"\n", data)
        parsed = hardened_parse(data, header="present")
        self.assertEqual(parsed.header, ("name",))
        self.assertEqual(parsed.records, (("N-1\nS-2",),))
        self.assertEqual(parsed.events, ())
