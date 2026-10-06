import unittest

import helpers  # noqa: F401

from edition_gate.decode import decode_bytes
from edition_gate.model import column, dialect, schema
from edition_gate.pipeline import ingest_bytes
from edition_gate.registry import Registry


class DecodeTests(unittest.TestCase):
    def test_bom_overrides_declared_utf16(self):
        body = "supplier_id,sku\nSUP1,A\n".encode("utf-8")
        decoded = decode_bytes(b"\xef\xbb\xbf" + body, "utf-16")
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["encoding"], "utf-8")
        self.assertTrue(decoded["charset_overridden_by_bom"])
        self.assertTrue(decoded["text"].startswith("supplier_id"))

    def test_utf16_le_bom_overrides_utf8_declaration(self):
        body = "a,b\n1,2\n".encode("utf-16-le")
        decoded = decode_bytes(b"\xff\xfe" + body, "utf-8")
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["encoding"], "utf-16-le")
        self.assertTrue(decoded["charset_overridden_by_bom"])
        self.assertIn("a,b", decoded["text"])

    def test_utf16_label_with_a_utf16_bom_is_not_an_override(self):
        body = "a,b\n".encode("utf-16-be")
        decoded = decode_bytes(b"\xfe\xff" + body, "UTF-16")
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["encoding"], "utf-16-be")
        self.assertFalse(decoded["charset_overridden_by_bom"])
        self.assertEqual(decoded["text"], "a,b\n")

    def test_illegal_utf8_is_fatal_and_not_replacement(self):
        self.assertEqual(b"\xff".decode("utf-8", errors="replace"), "\ufffd")
        decoded = decode_bytes(b"a,\xff\n", "utf-8")
        self.assertFalse(decoded["ok"])
        self.assertEqual(decoded["reason"], "decode_fatal")
        self.assertIsNone(decoded["text"])

    def test_missing_charset_does_not_guess_windows_1252(self):
        raw = "price \u201cquote\u201d".encode("windows-1252")
        self.assertIn(b"\x93", raw)
        decoded = decode_bytes(raw, None)
        self.assertEqual(decoded["reason"], "charset_missing")
        self.assertIsNone(decoded["text"])

    def test_declared_windows_1252_is_accepted(self):
        raw = "price \u201cquote\u201d".encode("windows-1252")
        decoded = decode_bytes(raw, "windows-1252")
        self.assertTrue(decoded["ok"])
        self.assertIn("\u201cquote\u201d", decoded["text"])

    def test_utf16_without_bom_is_fatal(self):
        decoded = decode_bytes("a,b\n".encode("utf-8"), "utf-16")
        self.assertEqual(decoded["reason"], "decode_fatal")
        self.assertIsNone(decoded["text"])

    def test_nul_is_binary_payload(self):
        decoded = decode_bytes(b"a\x00b", "utf-8")
        self.assertEqual(decoded["reason"], "binary_payload")

    def test_declared_cp932_round_trip(self):
        text = "supplier_id,title\nSUP1,ボルト\n"
        decoded = decode_bytes(text.encode("cp932"), "cp932")
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["text"], text)
        self.assertFalse(decoded["charset_overridden_by_bom"])

    def test_ingest_fatal_decode_emits_no_rows_and_logs(self):
        registry = Registry()
        self.assertTrue(registry.register(schema(1, [column("sku", required=True)]), [])["ok"])
        with self.assertLogs("edition_gate", level="INFO") as logs:
            result = ingest_bytes(registry, b"sku\n\xff\n", writer_version=1, link=False)
        self.assertEqual(result["status"], "quarantine")
        self.assertEqual(result["reason"], "decode_fatal")
        self.assertEqual(result["rows"], [])
        self.assertTrue(any("decode_fatal" in line for line in logs.output))

    def test_absent_charset_quarantines_before_parse(self):
        registry = Registry()
        built = schema(1, [column("sku")], dialect=dialect(charset=None))
        self.assertTrue(registry.register(built, [])["ok"])
        result = ingest_bytes(registry, b"sku\nA\n", writer_version=1, link=False)
        self.assertEqual(result["reason"], "charset_missing")
        self.assertEqual(result["rows"], [])


if __name__ == "__main__":
    unittest.main()
