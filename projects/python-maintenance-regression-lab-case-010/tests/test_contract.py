"""Public default, frozen decisions, and the boundary with the stdlib reader."""

from __future__ import annotations

import csv
import io
import unittest

import helpers  # noqa: F401
from helpers import CORPUS, LIBRARY, library_text

from wharf_sheet.decisions import freeze_pairs
from wharf_sheet.model import ParseFailure
from wharf_sheet.recognize import hardened_parse, legacy_parse, parse


PINNED = (
    ("D-crlf", "crlf-only", "repair"),
    ("D-empty-field", "accept-empty-field", "same"),
    ("D-spaces", "keep-spaces", "same"),
    ("D-header", "caller-policy", "same"),
    ("D-dup-header", "reject", "repair"),
    ("D-ragged", "reject", "repair"),
    ("D-quotes", "doubled-quote-only", "repair"),
    ("D-unicode", "accept-scalar", "same"),
    ("D-comments", "reject", "repair"),
    ("D-limit", "reject-over-limit", "same"),
    ("D-empty", "reject-zero-records", "same"),
    ("D-bom", "keep-as-text", "repair"),
    ("D-decode", "fatal", "repair"),
    ("D-charset", "closed-label-set", "same"),
    ("D-comma", "comma-only", "same"),
    ("D-digits", "keep-digit-string", "same"),
)


class ContractTests(unittest.TestCase):
    def test_public_default_is_the_hardened_parser(self):
        self.assertIs(parse, hardened_parse)
        data = (CORPUS / "b_comment_skipped.bin").read_bytes()
        self.assertEqual(parse(data), hardened_parse(data))
        self.assertNotEqual(parse(data), legacy_parse(data))
        accepted = b"sku,qty\r\nA1,2\r\n"
        self.assertEqual(parse(accepted), parse(accepted))

    def test_freeze_pin_matches_the_decision_table(self):
        self.assertEqual(freeze_pairs(), PINNED)

    def test_stdlib_reader_accepts_a_bare_line_feed(self):
        text = "a,b\nc,d\r\n"
        rows = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(rows, [["a", "b"], ["c", "d"]])
        result = hardened_parse(text.encode("utf-8"))
        self.assertIsInstance(result, ParseFailure)
        self.assertEqual(result.code, "E-bare-lf")
        self.assertEqual(result.decision_id, "D-crlf")

    def test_library_does_not_delegate_the_grammar_to_csv(self):
        text = library_text()
        for token in ("Sniffer", "QUOTE_NONNUMERIC", "QUOTE_STRINGS", "field_size_limit", "float("):
            self.assertNotIn(token, text)
        unparse_source = (LIBRARY / "unparse.py").read_text(encoding="utf-8")
        self.assertIn('newline=""', unparse_source)

    def test_text_after_a_closing_quote_fails_before_escape_handling(self):
        # A backslash after the closing quote must not let legacy append text to the field.
        for fn in (hardened_parse, legacy_parse):
            result = fn(b'"a"\\,b\r\n')
            self.assertIsInstance(result, ParseFailure)
            self.assertEqual((result.code, result.byte_offset), ("E-quote-tail", 3))

    def test_every_legacy_duplicate_header_name_is_an_event(self):
        result = legacy_parse(b"a,a,a\r\n1,2,3\r\n", header="present")
        self.assertEqual(result.header, ("a", "a", "a"))
        self.assertEqual(
            [(event.decision_id, event.field_index, event.byte_offset) for event in result.events],
            [("D-dup-header", 1, 2), ("D-dup-header", 2, 4)],
        )

    def test_quoted_newline_keeps_the_logical_record_index(self):
        result = hardened_parse(b'"a","b\r\nbb","c"\r\nx\r\n')
        self.assertIsInstance(result, ParseFailure)
        self.assertEqual(result.code, "E-ragged")
        self.assertEqual(result.decision_id, "D-ragged")
        self.assertEqual(result.record_index, 1)
