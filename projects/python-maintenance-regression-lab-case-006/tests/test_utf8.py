"""Strict UTF-8 vectors. Illegal sequences raise; accepted sequences round-trip."""

from __future__ import annotations

import random
import unittest

import helpers  # noqa: F401

from netunicode_lab.errors import StrictUtf8Error
from netunicode_lab.legacy import naive_forbidden_scalar
from netunicode_lab.utf8strict import _first_bad, check_strict_utf8

ILLEGAL = [
    ("overlong-nul", b"\xc0\x80", "\x00"),
    ("overlong-slash", b"\xc0\xaf", "/"),
    ("surrogate-bytes", bytes.fromhex("eda18cedbeb4"), "\U000233b4"),
    ("lead-c1", b"\xc1\xbf", None),
    ("lead-f5", b"\xf5\x80\x80\x80", None),
    ("lead-ff", b"\xff", None),
    ("truncated-e-acute", b"\xc3", None),
    ("above-10ffff", b"\xf4\x90\x80\x80", None),
    ("five-octet-lead", b"\xf8\x80\x80\x80\x80", None),
    ("bare-continuation", b"\x80", None),
    ("overlong-three", b"\xe0\x9f\xbf", None),
    ("surrogate-ed", b"\xed\xa0\x80", None),
    ("charmap-undefined", b"\x81", None),
]

ACCEPTED_CODEPOINTS = [
    0x00,
    0x7F,
    0x80,
    0x7FF,
    0x800,
    0xD7FF,
    0xE000,
    0xFFFF,
    0x10000,
    0x10FFFF,
]


class StrictUtf8Tests(unittest.TestCase):
    def test_illegal_vectors_raise_and_are_not_the_forbidden_scalar(self):
        for name, blob, forbidden in ILLEGAL:
            with self.subTest(vector=name):
                with self.assertRaises(UnicodeDecodeError):
                    blob.decode("utf-8")
                with self.assertRaises(StrictUtf8Error) as caught:
                    check_strict_utf8(blob)
                message = str(caught.exception)
                self.assertIn("strict-utf8", message)
                self.assertIn(f"0x{caught.exception.bad_byte:02x}", message)
                if forbidden is not None:
                    self.assertEqual(naive_forbidden_scalar(blob), forbidden)

    def test_overlong_nul_does_not_decode_to_nul(self):
        with self.assertRaises(StrictUtf8Error) as caught:
            check_strict_utf8(b"\xc0\x80")
        self.assertEqual(caught.exception.bad_byte, 0xC0)
        self.assertEqual(naive_forbidden_scalar(b"\xc0\x80"), "\x00")

    def test_overlong_slash_does_not_decode_to_slash(self):
        with self.assertRaises(StrictUtf8Error) as caught:
            check_strict_utf8(b"\xc0\xaf")
        self.assertEqual(caught.exception.bad_byte, 0xC0)
        self.assertEqual(naive_forbidden_scalar(b"\xc0\xaf"), "/")

    def test_cesu_surrogate_bytes_do_not_decode_to_the_supplementary_scalar(self):
        blob = bytes.fromhex("eda18cedbeb4")
        with self.assertRaises(StrictUtf8Error) as caught:
            check_strict_utf8(blob)
        # ED is a legal lead; A1 is the first byte outside the ED 80-9F range.
        self.assertEqual(caught.exception.bad_byte, 0xA1)
        self.assertEqual(caught.exception.offset, 1)
        self.assertEqual(naive_forbidden_scalar(blob), "\U000233b4")

    def test_accepted_codepoints_round_trip(self):
        for code in ACCEPTED_CODEPOINTS:
            blob = chr(code).encode("utf-8")
            with self.subTest(code=hex(code)):
                text = check_strict_utf8(blob)
                self.assertEqual(text.encode("utf-8"), blob)
                self.assertEqual(text, chr(code))

    def test_e_acute_and_emoji_round_trip(self):
        for blob in (b"\xc3\xa9", "\U0001f600".encode("utf-8"), b""):
            with self.subTest(blob=blob):
                self.assertEqual(check_strict_utf8(blob).encode("utf-8"), blob)

    def test_byte_walker_agrees_with_cpython_strict_decoder(self):
        # The walker reports the bad byte; it must not accept or reject differently.
        generator = random.Random(3629)
        alphabet = [0x00, 0x41, 0x7F, 0x80, 0x8F, 0x90, 0x9F, 0xA0, 0xBF, 0xC0, 0xC1, 0xC2,
                    0xDF, 0xE0, 0xE1, 0xEC, 0xED, 0xEE, 0xEF, 0xF0, 0xF1, 0xF3, 0xF4, 0xF5, 0xFF]
        for _ in range(5000):
            blob = bytes(generator.choice(alphabet) for _ in range(generator.randint(1, 6)))
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError:
                cpython_ok = False
            else:
                cpython_ok = True
            with self.subTest(blob=blob.hex()):
                self.assertEqual(_first_bad(blob) is None, cpython_ok)

    def test_checker_accepts_a_leading_zwnbsp_as_text(self):
        self.assertEqual(check_strict_utf8(b"\xef\xbb\xbfa"), "\ufeffa")


if __name__ == "__main__":
    unittest.main()
