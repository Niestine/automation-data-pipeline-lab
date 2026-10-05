"""Foreign ingest: logical lines, BOM refusal, and the charmap contrast."""

from __future__ import annotations

import logging
import re
import unittest

import helpers  # noqa: F401
from helpers import MIXED_NEWLINES, LabTest

from netunicode_lab.errors import LeadingBomError, StrictUtf8Error
from netunicode_lab.ingest import logical_lines, read_foreign_text
from netunicode_lab.legacy import read_cp1252, split_on_lf_only

logging.getLogger("netunicode_lab").setLevel(logging.WARNING)


class IngestTests(LabTest):
    def test_utf8_e_acute(self):
        path = self.root / "e.bin"
        path.write_bytes(b"\xc3\xa9")
        self.assertEqual(read_foreign_text(path), "\u00e9")
        self.assertEqual(read_foreign_text(path).encode("utf-8"), b"\xc3\xa9")

    def test_mojibake_contrast_is_not_what_production_reads(self):
        path = self.root / "e.bin"
        path.write_bytes(b"\xc3\xa9")
        cp1252 = b"\xc3\xa9".decode("cp1252")
        cp932 = b"\xc3\xa9".decode("cp932")
        self.assertEqual(cp1252, "\u00c3\u00a9")
        self.assertNotEqual(cp1252, "\u00e9")
        self.assertEqual(cp932, "\uff83\uff69")
        self.assertNotEqual(cp932, "\u00e9")
        self.assertEqual(read_foreign_text(path), "\u00e9")

    def test_charmap_undefined_byte_and_production_strict_utf8(self):
        path = self.root / "hole.bin"
        path.write_bytes(b"AB\x81CD")
        with self.assertRaises(UnicodeDecodeError) as legacy:
            read_cp1252(path)
        self.assertIn("'charmap' codec can't decode byte 0x81", str(legacy.exception))
        with self.assertLogs("netunicode_lab", level="WARNING") as captured:
            with self.assertRaises(StrictUtf8Error) as production:
                read_foreign_text(path)
        self.assertEqual(
            str(production.exception),
            "strict-utf8 rejected byte 0x81 at offset 2",
        )
        self.assertNotIn("charmap", str(production.exception))
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertIn("boundary=foreign-ingest", message)
        self.assertIn("codec=strict-utf8", message)
        self.assertIn("byte=0x81", message)
        self.assertNotIn("AB", message)
        self.assertNotIn("CD", message)
        self.assertNotIn(path.name, message)

    def test_crlf_lf_cr_become_four_logical_lines(self):
        path = self.root / "mixed.bin"
        path.write_bytes(MIXED_NEWLINES)
        self.assertEqual(read_foreign_text(path), "a\nb\nc\nd")
        self.assertEqual(logical_lines("a\r\nb\nc\rd"), ["a", "b", "c", "d"])

    def test_regex_matches_the_first_logical_line(self):
        path = self.root / "rows.bin"
        path.write_bytes(b"row1\r\nrow2")
        line = read_foreign_text(path).split("\n")[0]
        self.assertIsNotNone(re.fullmatch(r"row1", line))
        buggy = split_on_lf_only(b"row1\r\nrow2".decode("utf-8"))[0]
        self.assertIsNone(re.fullmatch(r"row1", buggy))
        self.assertTrue(buggy.endswith("\r"))

    def test_nel_and_unicode_separators_stay_inside_one_line(self):
        samples = ["a\u0085b", "a\u2028b", "a\u2029b"]
        for text in samples:
            with self.subTest(text=text):
                path = self.root / "line.bin"
                path.write_bytes(text.encode("utf-8"))
                self.assertNotEqual("\n".join(text.splitlines()), text)
                self.assertEqual(read_foreign_text(path), text)
                self.assertNotIn("\n", read_foreign_text(path))

    def test_leading_bom_is_its_own_error(self):
        path = self.root / "bom.bin"
        path.write_bytes(b"\xef\xbb\xbfa")
        with self.assertLogs("netunicode_lab", level="WARNING") as captured:
            with self.assertRaises(LeadingBomError) as caught:
                read_foreign_text(path)
        self.assertIn("leading BOM", str(caught.exception))
        self.assertIn("strict-utf8", str(caught.exception))
        self.assertIn("byte=0xef", captured.records[0].getMessage())
        self.assertIn("boundary=foreign-ingest", captured.records[0].getMessage())

    def test_trailing_crlf_becomes_one_lf(self):
        path = self.root / "trail.bin"
        path.write_bytes(b"a\r\n")
        self.assertEqual(read_foreign_text(path), "a\n")


if __name__ == "__main__":
    unittest.main()
