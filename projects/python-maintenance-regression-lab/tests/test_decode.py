import unittest

import helpers  # noqa: F401
from helpers import EXAMPLES

from maintenance_lab.decode import decode_feed, looks_japanese, peek_preamble_hints
from maintenance_lab.errors import DecodeError
from maintenance_lab.seed import JP_CSV, LATIN1_CSV, jp_cp932_bytes, latin1_bytes


class DecodeTests(unittest.TestCase):
    def test_utf8_roundtrip(self):
        result = decode_feed("sku,title\nA,B\n".encode("utf-8"))
        self.assertEqual(result.encoding, "utf-8")
        self.assertEqual(result.bug_guards, ())

    def test_utf8_bom(self):
        result = decode_feed(b"\xef\xbb\xbfsku,title\nA,B\n")
        self.assertEqual(result.encoding, "utf-8-sig")
        self.assertTrue(result.text.startswith("sku"))

    def test_cp932_japanese(self):
        result = decode_feed(jp_cp932_bytes())
        self.assertEqual(result.encoding, "cp932")
        self.assertIn("BUG-001", result.bug_guards)
        self.assertIn("木綿シャツ", result.text)

    def test_latin1_cafe_not_treated_as_japanese(self):
        result = decode_feed(latin1_bytes())
        self.assertEqual(result.encoding, "latin-1")
        self.assertIn("BUG-016", result.bug_guards)
        self.assertIn("Café", result.text)
        self.assertFalse(looks_japanese(result.text))

    def test_declared_encoding_used(self):
        result = decode_feed(jp_cp932_bytes(), declared_encoding="cp932")
        self.assertEqual(result.encoding, "cp932")
        self.assertIn("木綿シャツ", result.text)

    def test_preamble_encoding_hint(self):
        hints = peek_preamble_hints(jp_cp932_bytes())
        self.assertEqual(hints.get("encoding"), "cp932")
        self.assertEqual(hints.get("currency"), "JPY")

    def test_declared_latin1_is_not_tagged_as_shift_jis(self):
        result = decode_feed(latin1_bytes(), declared_encoding="latin-1")
        self.assertIn("Café", result.text)
        self.assertEqual(result.bug_guards, ("BUG-016",))

    def test_empty_feed_fails(self):
        with self.assertRaises(DecodeError):
            decode_feed(b"")

    def test_unknown_declared_encoding(self):
        with self.assertRaises(DecodeError):
            decode_feed(b"abc", declared_encoding="not-an-encoding")

    def test_jp_source_text_matches_seed(self):
        self.assertIn("絹のスカーフ", JP_CSV)
        self.assertIn("Café", LATIN1_CSV)

    def test_examples_jp_file_is_readable_utf8(self):
        text = (EXAMPLES / "feeds" / "supplier_v1_jp.csv").read_text(encoding="utf-8")
        self.assertIn("木綿シャツ", text)


if __name__ == "__main__":
    unittest.main()
