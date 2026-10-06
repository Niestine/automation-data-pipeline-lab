"""Decode wrapper: fatal versus replacement, BOM strip, and the closed label set."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from wharf_sheet.decode import lookup_label
from wharf_sheet.model import ParseFailure, ParseSuccess
from wharf_sheet.recognize import hardened_parse, legacy_parse


class DecodeTests(unittest.TestCase):
    def test_replacement_keeps_the_comma_and_fatal_skips_the_handler(self):
        seen = []
        fatal = hardened_parse(b"\xff,", handler=seen.append)
        self.assertIsInstance(fatal, ParseFailure)
        self.assertEqual(fatal.code, "E-decode")
        self.assertEqual(fatal.decision_id, "D-decode")
        self.assertEqual(seen, [])

        replaced = legacy_parse(b"\xff,")
        self.assertIsInstance(replaced, ParseSuccess)
        self.assertEqual(replaced.records, (("\ufffd", ""),))
        self.assertEqual(len(replaced.records[0]), 2)

    def test_incomplete_prefix_does_not_swallow_the_comma(self):
        fatal = hardened_parse(b"\xef\xbb,")
        self.assertEqual(fatal.code, "E-decode")
        self.assertEqual(fatal.byte_offset, 0)
        replaced = legacy_parse(b"\xef\xbb,")
        self.assertEqual(replaced.records, (("\ufffd", ""),))
        self.assertFalse(replaced.bom_stripped)
        eaten = legacy_parse(b"\xef\xbb,", mutant="drop_ascii_restore")
        self.assertNotEqual(getattr(eaten, "records", None), replaced.records)

    def test_bom_flag_is_true_only_when_three_bytes_were_removed(self):
        leading = b"\xef\xbb\xbf" + b"a,b\r\n"
        legacy = legacy_parse(leading)
        self.assertTrue(legacy.bom_stripped)
        self.assertEqual(legacy.records, (("a", "b"),))
        self.assertNotIn("\ufeff", "".join(legacy.records[0]))
        self.assertEqual(legacy.events[0].decision_id, "D-bom")

        strict = hardened_parse(leading)
        self.assertFalse(strict.bom_stripped)
        self.assertEqual(strict.events, ())
        self.assertTrue(strict.records[0][0].startswith("\ufeff"))

        only = legacy_parse(b"\xef\xbb\xbf")
        self.assertEqual(only.code, "E-empty")
        self.assertTrue(only.bom_stripped)
        self.assertEqual(only.byte_offset, 3)

        incomplete = legacy_parse(b"\xef\xbb")
        self.assertFalse(incomplete.bom_stripped)

    def test_closed_label_set(self):
        self.assertEqual(lookup_label("  UTF-8\n"), "UTF-8")
        self.assertEqual(lookup_label("utf8"), "UTF-8")
        self.assertEqual(lookup_label("ascii"), "windows-1252")
        self.assertIsNone(lookup_label("utf8-bom"))
        # Matching is ASCII case-insensitive. KELVIN SIGN lowers to "k" under str.lower.
        self.assertEqual(lookup_label("KOI8-R"), "KOI8-R")
        self.assertIsNone(lookup_label("Koi8-r"))

        unknown = hardened_parse(b"a,b\r\n", charset="utf8-bom")
        self.assertEqual(unknown.code, "E-charset")
        known_other = hardened_parse(b"a,b\r\n", charset="windows-1252")
        self.assertEqual(known_other.code, "E-not-utf8")
        self.assertEqual(known_other.decision_id, "D-charset")
        accepted = hardened_parse(b"a,b\r\n", charset="  UTF-8\n")
        self.assertEqual(accepted.records, (("a", "b"),))
