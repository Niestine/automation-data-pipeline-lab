"""Closed CSV profile and BOM-first decoding."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

import support
from harbor_ledger.csvio import Dialect, parse_csv, write_csv
from harbor_ledger.decode import DecodeError, FatalDecodeError, decode_owned, decode_supplier, output_encoding


class CsvProfileTests(unittest.TestCase):
    def test_writer_is_identical_across_processes(self) -> None:
        code = (
            "import os, sys\n"
            "sys.path.insert(0, os.environ['HARBOR_SRC'])\n"
            "from harbor_ledger.csvio import write_csv\n"
            "sys.stdout.buffer.write(write_csv(['a', 'b'], [['1', '2'], ['3', 'x,y']]))\n"
        )
        env = os.environ.copy()
        env["HARBOR_SRC"] = str(support.SRC)
        first = subprocess.check_output([sys.executable, "-c", code], env=env)
        second = subprocess.check_output([sys.executable, "-c", code], env=env)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\r\n"))
        self.assertFalse(first.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b'"x,y"', first)
        self.assertNotIn(b" ", first.split(b"\r\n")[0])

    def test_reader_accepts_lf_and_a_missing_final_break(self) -> None:
        blob = write_csv(["a", "b"], [["1", "2"], ["3", "x,y"]])
        without_break = blob[:-2]
        lf_only = blob.replace(b"\r\n", b"\n")
        dialect = Dialect(header=True)
        original = parse_csv(blob.decode("utf-8"), dialect, 2)
        trimmed = parse_csv(without_break.decode("utf-8"), dialect, 2)
        lf_parsed = parse_csv(lf_only.decode("utf-8"), dialect, 2)
        self.assertEqual(original.records[1].fields, ["3", "x,y"])
        self.assertEqual(trimmed.records[1].fields, original.records[1].fields)
        self.assertEqual(lf_parsed.records[0].fields, original.records[0].fields)

    def test_trim_is_off_unless_the_schema_opts_in(self) -> None:
        text = "a,b\n a ,x\n"
        kept = parse_csv(text, Dialect(header=True, trim=False), 2)
        trimmed = parse_csv(text, Dialect(header=True, trim=True), 2)
        self.assertEqual(kept.records[0].fields[0], " a ")
        self.assertEqual(kept.records[0].trims, [])
        self.assertEqual(trimmed.records[0].fields[0], "a")
        self.assertEqual(trimmed.records[0].trims[0]["before"], " a ")
        self.assertEqual(trimmed.records[0].trims[0]["after"], "a")

    def test_header_presence_is_taken_from_the_schema(self) -> None:
        text = "123,456\n789,012\n"
        headed = parse_csv(text, Dialect(header=True), 2)
        headless = parse_csv(text, Dialect(header=False), 2)
        self.assertEqual(headed.header, ["123", "456"])
        self.assertEqual(len(headed.records), 1)
        self.assertEqual(headless.header, None)
        self.assertEqual(headless.records[0].fields, ["123", "456"])
        self.assertEqual(len(headless.records), 2)

    def test_comment_and_blank_rows_keep_physical_numbers(self) -> None:
        text = "# note\n\nonly_one\n"
        parsed = parse_csv(text, Dialect(header=False, comment_prefix="#", skip_blank_rows=True), 3)
        self.assertEqual(parsed.records[0].source_row, 3)
        self.assertEqual(parsed.records[0].output_row, 1)
        self.assertIn("ragged", parsed.records[0].parse_errors)
        self.assertEqual(parsed.records[0].fields, ["only_one"])

    def test_short_row_does_not_steal_the_next_record(self) -> None:
        text = "a,b,c\n1,2\n3,4,5\n"
        parsed = parse_csv(text, Dialect(header=True), 3)
        self.assertEqual(parsed.records[0].fields, ["1", "2"])
        self.assertEqual(parsed.records[1].fields, ["3", "4", "5"])

    def test_quotes_round_trip_and_spaces_stay_unquoted(self) -> None:
        blob = write_csv(["name"], [[" a "], ['say "hi"'], ["line\nbreak"]])
        text = blob.decode("utf-8")
        self.assertIn(" a ", text)
        self.assertNotIn('" a "', text)
        parsed = parse_csv(text, Dialect(header=True), 1)
        self.assertEqual(parsed.records[0].fields, [" a "])
        self.assertEqual(parsed.records[1].fields, ['say "hi"'])
        self.assertEqual(parsed.records[2].fields, ["line\nbreak"])


class DecodeTests(unittest.TestCase):
    def test_bom_overrides_a_declared_shift_jis_label(self) -> None:
        raw = b"\xef\xbb\xbf" + "listing_id,title\r\nAB,Coat\r\n".encode("utf-8")
        decoded = decode_supplier(raw, "shift_jis", replacement_threshold=3)
        self.assertTrue(decoded.bom_override)
        self.assertEqual(decoded.encoding_used, "utf-8")
        self.assertFalse(decoded.text.startswith("\ufeff"))
        parsed = parse_csv(decoded.text, Dialect(header=True), 2)
        self.assertEqual(parsed.records[0].fields[0], "AB")

    def test_shift_jis_illegal_pair_does_not_swallow_a_quote(self) -> None:
        decoded = decode_supplier(bytes([0x82, 0x22]), "shift_jis", replacement_threshold=5)
        self.assertEqual(decoded.text, "\ufffd\"")
        self.assertEqual(decoded.replacement_count, 1)

    def test_latin1_label_decodes_byte_80_as_euro(self) -> None:
        decoded = decode_supplier(bytes([0x80]), "latin1", replacement_threshold=0)
        self.assertEqual(decoded.text, "\u20ac")
        self.assertEqual(decoded.replacement_count, 0)
        self.assertFalse(decoded.quarantined)

    def test_owned_file_with_a_bad_byte_fails_closed(self) -> None:
        with self.assertRaises(FatalDecodeError):
            decode_owned(b"\xff")
        with self.assertRaises(FatalDecodeError):
            decode_owned(b"\xef\xbb\xbfok")

    def test_unknown_label_and_quarantine_threshold(self) -> None:
        with self.assertRaises(DecodeError):
            decode_supplier(b"abc", "utf-16", replacement_threshold=5)
        decoded = decode_supplier(b"\xff\xff", "utf-8", replacement_threshold=1)
        self.assertTrue(decoded.quarantined)
        self.assertGreater(decoded.replacement_count, 1)
        allowed = decode_supplier(b"\xff", "utf-8", replacement_threshold=1)
        self.assertFalse(allowed.quarantined)

    def test_output_encoding_is_utf8(self) -> None:
        self.assertEqual(output_encoding("shift_jis"), "utf-8")
        self.assertEqual(output_encoding("utf-16be"), "utf-8")
        self.assertEqual(output_encoding("replacement"), "utf-8")


if __name__ == "__main__":
    unittest.main()
