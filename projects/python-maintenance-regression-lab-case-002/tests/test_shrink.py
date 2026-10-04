import unittest

import helpers  # noqa: F401

from slip_lab.dialect import is_dialect_valid
from slip_lab.differential import classify_inprocess
from slip_lab.shrink import is_local_minimum, make_interesting, shrink
from slip_lab.stock import QUOTE_ROW


class ShrinkTests(unittest.TestCase):
    def test_shrink_validity_and_shortlex(self):
        parent = b"ZZ|9\n" + QUOTE_ROW + b"PAD|0\n"
        interesting = make_interesting(parent, classify_inprocess)
        self.assertFalse(interesting(b'"""|1\n'))
        reduced = shrink(parent, interesting)
        self.assertEqual(reduced, b'""""')
        self.assertNotIn(b"ZZ", reduced)
        self.assertNotIn(b"PAD", reduced)
        self.assertTrue(is_dialect_valid(reduced))
        self.assertEqual(classify_inprocess(reduced).kind, "DISAGREE")
        self.assertEqual(shrink(reduced, interesting), reduced)
        self.assertTrue(is_local_minimum(reduced, interesting))
        self.assertNotEqual(reduced, b'"""|1\n')

        carriage = b"AA|1\r\nBB|2\n"
        carriage_interesting = make_interesting(carriage, classify_inprocess)
        shrunk = shrink(carriage, carriage_interesting)
        self.assertIn(b"\r", shrunk)
        self.assertLess(len(shrunk), len(carriage))
        self.assertFalse(is_dialect_valid(shrunk))
        outcome = classify_inprocess(shrunk)
        self.assertEqual(outcome.kind, "CRASH")
        self.assertEqual(outcome.side, "legacy")
        self.assertEqual(shrink(shrunk, carriage_interesting), shrunk)
        self.assertFalse(carriage_interesting(b"AA|1\n"))


if __name__ == "__main__":
    unittest.main()
