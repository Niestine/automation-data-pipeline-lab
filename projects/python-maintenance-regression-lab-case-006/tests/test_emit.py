"""Interchange emit: golden bytes, NFC pairs, and profile rejects."""

from __future__ import annotations

import sys
import unicodedata
import unittest
from unittest import mock

import helpers  # noqa: F401
from helpers import GOLDEN_BYTES, GOLDEN_PATH, GOLDEN_ROWS, GOLDEN_SHA256, LabTest, sha256

from netunicode_lab import assert_interchange_bytes, emit_interchange_csv
from netunicode_lab.errors import LeadingBomError, ProfileError, StrictUtf8Error
from netunicode_lab.legacy import double_translate_newlines
from netunicode_lab.policy import ENCODING, ERRORS, OPEN_NEWLINE


class EmitTests(LabTest):
    def test_golden_file_matches_the_literal_oracle(self):
        self.assertEqual(GOLDEN_PATH.read_bytes(), GOLDEN_BYTES)
        self.assertEqual(sha256(GOLDEN_BYTES), GOLDEN_SHA256)
        self.assertFalse(GOLDEN_BYTES.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(GOLDEN_BYTES.count(b"\r\r\n"), 0)
        self.assertEqual(GOLDEN_BYTES.count(b"\r\n"), 2)
        assert_interchange_bytes(GOLDEN_BYTES)

    def test_emit_matches_the_golden_digest(self):
        destination = self.root / "rows.csv"
        emit_interchange_csv(destination, GOLDEN_ROWS)
        data = destination.read_bytes()
        self.assertEqual(data, GOLDEN_BYTES)
        self.assertEqual(sha256(data), GOLDEN_SHA256)
        emit_interchange_csv(destination, GOLDEN_ROWS)
        self.assertEqual(destination.read_bytes(), GOLDEN_BYTES)

    def test_double_translation_fails_the_profile(self):
        broken = double_translate_newlines(GOLDEN_BYTES)
        self.assertIn(b"\r\r\n", broken)
        self.assertNotEqual(broken, GOLDEN_BYTES)
        with self.assertLogs("netunicode_lab", level="WARNING") as captured:
            with self.assertRaises(ProfileError):
                assert_interchange_bytes(broken)
        message = captured.records[0].getMessage()
        self.assertIn("boundary=interchange-emit", message)
        self.assertIn("byte=0x0d", message)

    @unittest.skipUnless(
        __import__("os").linesep == "\r\n",
        "text-mode translation doubles CR only when os.linesep is CR LF",
    )
    def test_text_open_without_newline_doubles_cr_on_windows(self):
        import csv

        path = self.root / "mutant.csv"
        with path.open("w", encoding="utf-8") as handle:
            csv.writer(handle, dialect="excel").writerow(["a", "b"])
        data = path.read_bytes()
        self.assertIn(b"\r\r\n", data)
        self.assertNotIn(b"\r\r\n", GOLDEN_BYTES)

    def test_forced_windows_newline_translation_is_rejected_on_any_os(self):
        # newline="\r\n" is what a Windows text open without newline="" does.
        destination = self.root / "rows.csv"
        with mock.patch("netunicode_lab.emit.OPEN_NEWLINE", "\r\n"):
            with self.assertLogs("netunicode_lab", level="WARNING") as captured:
                with self.assertRaises(ProfileError) as caught:
                    emit_interchange_csv(destination, GOLDEN_ROWS)
        self.assertIn("CR CR LF", str(caught.exception))
        self.assertIn("byte=0x0d", captured.records[0].getMessage())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_forced_ansi_codec_cannot_produce_the_golden_file(self):
        # cp1252 is the codec a Windows ANSI locale open would pick.
        destination = self.root / "rows.csv"
        with mock.patch("netunicode_lab.emit.ENCODING", "cp1252"):
            with self.assertRaises(UnicodeEncodeError):
                emit_interchange_csv(destination, GOLDEN_ROWS)
            with self.assertLogs("netunicode_lab", level="WARNING") as captured:
                with self.assertRaises(StrictUtf8Error) as caught:
                    emit_interchange_csv(destination, [["caf\u00e9"]])
        # cp1252 wrote E9 as a lone lead; the walker names the CR that breaks the sequence.
        self.assertEqual((caught.exception.offset, caught.exception.bad_byte), (4, 0x0D))
        message = captured.records[0].getMessage()
        self.assertIn("boundary=interchange-emit", message)
        self.assertIn("codec=strict-utf8", message)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_nfc_ohm_and_omega_share_one_body(self):
        ohm = self._emit([["\u2126"]])
        omega = self._emit([["\u03a9"]])
        body = unicodedata.normalize("NFC", "\u2126").encode("utf-8")
        self.assertEqual(ohm, omega)
        self.assertEqual(ohm[:-2], body)
        self.assertEqual(omega[:-2], unicodedata.normalize("NFC", "\u03a9").encode("utf-8"))
        self.assertTrue(ohm.endswith(b"\r\n"))
        self.assertNotIn("\u2126".encode("utf-8"), ohm)
        self.assertEqual(unicodedata.normalize("NFC", unicodedata.normalize("NFC", "\u2126")), "\u03a9")

    def test_nfc_grave_pair_shares_one_body(self):
        composed = self._emit([["\u00e0"]])
        combining = self._emit([["a\u0300"]])
        body = unicodedata.normalize("NFC", "a\u0300").encode("utf-8")
        self.assertEqual(composed, combining)
        self.assertEqual(composed[:-2], body)
        self.assertEqual(combining[:-2], unicodedata.normalize("NFC", "\u00e0").encode("utf-8"))
        self.assertNotIn(b"\xcc\x80", combining)
        self.assertIn(b"\xc3\xa0", combining)

    def test_nel_field_is_rejected_and_writes_nothing(self):
        destination = self.root / "nel.csv"
        with self.assertLogs("netunicode_lab", level="WARNING") as captured:
            with self.assertRaises(ProfileError) as caught:
                emit_interchange_csv(destination, [["a\u0085b"]])
        self.assertIn("U+0085", str(caught.exception))
        self.assertIn("utf-8", str(caught.exception))
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIn("boundary=interchange-emit", captured.records[0].getMessage())
        self.assertIn("codec=utf-8", captured.records[0].getMessage())

    def test_lone_surrogate_is_rejected(self):
        destination = self.root / "surrogate.csv"
        with self.assertRaises(ProfileError) as caught:
            emit_interchange_csv(destination, [["\ud800"]])
        self.assertIn("utf-8", str(caught.exception))
        self.assertFalse(destination.exists())

    def test_unassigned_logs_the_interpreter_unicode_version(self):
        character = "\u0378"
        self.assertEqual(
            unicodedata.category(character),
            "Cn",
            unicodedata.unidata_version,
        )
        destination = self.root / "cn.csv"
        with self.assertLogs("netunicode_lab", level="WARNING") as captured:
            with self.assertRaises(ProfileError) as caught:
                emit_interchange_csv(destination, [[character]])
        message = captured.records[0].getMessage()
        self.assertIn("boundary=interchange-emit", message)
        self.assertIn("codec=nfc", message)
        self.assertIn(f"unidata_version={unicodedata.unidata_version}", message)
        self.assertIn("U+0378", str(caught.exception))
        self.assertFalse(destination.exists())
        self.assertEqual(len(captured.records), 1)

    def test_leading_bom_profile_and_field(self):
        with self.assertRaises(LeadingBomError) as caught:
            assert_interchange_bytes(b"\xef\xbb\xbfa")
        self.assertIn("leading BOM", str(caught.exception))
        destination = self.root / "bom.csv"
        with self.assertRaises(LeadingBomError):
            emit_interchange_csv(destination, [["\ufeff"]])
        self.assertFalse(destination.exists())

    def test_middle_zwnbsp_is_not_a_signature(self):
        assert_interchange_bytes("a\ufeff\r\n".encode("utf-8"))

    def test_c1_bytes_fail_the_profile_even_when_utf8_is_well_formed(self):
        blob = "a\u0085b\r\n".encode("utf-8")
        with self.assertRaises(ProfileError) as caught:
            assert_interchange_bytes(blob)
        self.assertIn("U+0085", str(caught.exception))

    def test_csv_quoting_is_real(self):
        data = self._emit([["a,b", "c"], ['say "hi"']])
        self.assertEqual(data, b'"a,b",c\r\n"say ""hi"""\r\n')
        assert_interchange_bytes(data)

    def test_line_break_inside_a_field_writes_nothing(self):
        destination = self.root / "break.csv"
        with self.assertRaises(ProfileError) as caught:
            emit_interchange_csv(destination, [["a\nb"]])
        self.assertIn("0x0a", str(caught.exception))
        self.assertFalse(destination.exists())

    def test_fields_must_be_str(self):
        with self.assertRaises(TypeError):
            emit_interchange_csv(self.root / "bad.csv", [[1]])
        self.assertFalse((self.root / "bad.csv").exists())

    def test_policy_constants_are_the_wire_contract(self):
        self.assertEqual(ENCODING, "utf-8")
        self.assertEqual(ERRORS, "strict")
        self.assertEqual(OPEN_NEWLINE, "")

    def test_emit_call_graph_does_not_include_diagnose(self):
        seen = []

        def trace(frame, event, arg):
            if event == "call":
                seen.append(frame.f_code.co_name)
            return trace

        destination = self.root / "rows.csv"
        sys.settrace(trace)
        try:
            emit_interchange_csv(destination, GOLDEN_ROWS)
        finally:
            sys.settrace(None)
        self.assertNotIn("diagnose_mojibake", seen)

    def _emit(self, rows) -> bytes:
        destination = self.root / "one.csv"
        if destination.exists():
            destination.unlink()
        emit_interchange_csv(destination, rows)
        return destination.read_bytes()


if __name__ == "__main__":
    unittest.main()
