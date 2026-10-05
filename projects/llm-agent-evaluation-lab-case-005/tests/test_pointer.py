"""RFC 6901 pointer escapes and index rules."""

from __future__ import annotations

import unittest

from helpers import SRC  # noqa: F401  (puts src on sys.path)
from repair_gate.pointer import PointerError, decode_token, parse_pointer, pointer_get
from repair_gate.util import json_equal


class PointerTests(unittest.TestCase):
    def test_tilde_one_is_decoded_before_tilde_zero(self):
        # ~1 is applied before ~0, so "~01" is a tilde plus a literal 1.
        self.assertEqual(decode_token("~01"), "~1")
        self.assertNotEqual(decode_token("~01"), "/")
        self.assertEqual(decode_token("~10"), "/0")
        self.assertEqual(decode_token("~0"), "~")
        self.assertEqual(decode_token("~1"), "/")
        self.assertEqual(parse_pointer("/~0"), ["~"])
        self.assertEqual(parse_pointer("/~1"), ["/"])

    def test_bad_pointer_and_dash_are_rejected(self):
        with self.assertRaises(PointerError):
            parse_pointer("a")
        with self.assertRaises(PointerError) as raised:
            pointer_get({"a": [1]}, "/a/-")
        self.assertEqual(raised.exception.reason, "bad_index")

    def test_numbers_match_and_booleans_do_not(self):
        self.assertTrue(json_equal(1, 1.0))
        self.assertFalse(json_equal(True, 1))
        self.assertFalse(json_equal(False, 0))
        self.assertTrue(json_equal({"b": 1, "a": [True]}, {"a": [True], "b": 1.0}))


if __name__ == "__main__":
    unittest.main()
