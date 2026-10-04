import unittest

import helpers  # noqa: F401

from slip_lab.dialect import canonical, is_records
from slip_lab.hardened import parse as hardened_parse
from slip_lab.metamorphic import check_boundary, check_permutation, check_quote_round_trip, check_success
from slip_lab.stock import SPECS


def _drop_row_mutant(blob: bytes):
    value = hardened_parse(blob)
    if not is_records(value):
        return value
    _kind, header, rows = canonical(value)
    rows = list(rows)
    if len(rows) >= 2 and rows != sorted(rows):
        rows = rows[:-1]
    return ("records", header, tuple(rows))


def _mishandle_quote(blob: bytes):
    if b'"' not in blob:
        return hardened_parse(blob)
    text = blob.decode("latin-1").strip("\n")
    rows = tuple(tuple(line.split("|")) for line in text.split("\n") if line != "")
    return ("records", None, rows)


def _shift_neighbor(blob: bytes):
    if blob.decode("latin-1").replace("\n", "") == "ALPHB|AETA":
        return ("records", None, (("ALPHA", "BETA"),))
    return hardened_parse(blob)


class MetamorphicTests(unittest.TestCase):
    def test_quote_round_trip_and_permutation(self):
        successes = []
        before = {spec["id"]: spec["blob"] for spec in SPECS}
        for spec in SPECS:
            value = hardened_parse(spec["blob"])
            if spec["id"] == "SLIP-005":
                continue
            if is_records(value) and canonical(value)[2]:
                report = check_success(hardened_parse, value, full=True)
                self.assertEqual(report.violations, 0, spec["id"])
                successes.append(spec["id"])
        self.assertIn("CASE-two-lanes", successes)
        self.assertEqual(before, {spec["id"]: spec["blob"] for spec in SPECS})

        lanes = ("records", None, (("L1", "1"), ("L2", "2"), ("L3", "3")))
        dropped = check_permutation(_drop_row_mutant, lanes, full=True)
        self.assertGreater(dropped.violations, 0)
        self.assertIsInstance(_drop_row_mutant(b"L3|3\nL2|2\nL1|1\n"), tuple)

        quoted = ("records", None, (("A|B", "1"),))
        broken = check_quote_round_trip(_mishandle_quote, quoted)
        self.assertEqual(broken.violations, 1)
        self.assertIsInstance(_mishandle_quote(b'"A|B"|1\n'), tuple)
        self.assertEqual(check_quote_round_trip(hardened_parse, quoted).violations, 0)

        def fail_only_reverse(blob: bytes):
            if blob.decode("latin-1").startswith("L3|"):
                return ("records", None, ())
            return hardened_parse(blob)

        def fail_other_order(blob: bytes):
            if blob.decode("latin-1").startswith("L2|2\nL1|"):
                return ("records", None, ())
            return hardened_parse(blob)

        self.assertGreater(check_permutation(fail_only_reverse, lanes, full=False).violations, 0)
        self.assertEqual(check_permutation(fail_other_order, lanes, full=False).violations, 0)
        self.assertGreater(check_permutation(fail_other_order, lanes, full=True).violations, 0)

    def test_neighbor_catches_split(self):
        seed = ("records", None, (("ALPHA", "BETA"),))
        self.assertEqual(
            canonical(_shift_neighbor(b"ALPHA|BETA\n")),
            ("records", None, (("ALPHA", "BETA"),)),
        )
        caught = check_boundary(_shift_neighbor, seed, full=True)
        self.assertGreater(caught.violations, 0)
        self.assertIn("boundary-swap", caught.notes)
        self.assertEqual(check_boundary(hardened_parse, seed, full=True).violations, 0)

        def drop_second_probe(blob: bytes):
            if blob.decode("latin-1").strip() == "BETA":
                return ("records", None, (("NO",),))
            return hardened_parse(blob)

        self.assertEqual(check_boundary(drop_second_probe, seed, full=False).violations, 0)
        self.assertGreater(check_boundary(drop_second_probe, seed, full=True).violations, 0)
        skipped = check_boundary(hardened_parse, ("records", None, (("", "X"),)), full=True)
        self.assertEqual(skipped.skips, 1)
        self.assertEqual(skipped.violations, 0)

    def test_header_mark_rows_are_skipped_not_failed(self):
        for blob in (b"A|B\n@slip|C\n", b"@slip|a|b\n@slip|x\n", b"@slip|a|b\n@sliX|pY\n"):
            value = hardened_parse(blob)
            self.assertTrue(is_records(value), blob)
            report = check_success(hardened_parse, value, full=True)
            self.assertEqual(report.violations, 0, (blob, report.notes))
            self.assertGreater(report.skips, 0, blob)

    def test_crashing_follow_up_is_a_violation(self):
        def crash_on_reorder(blob: bytes):
            if blob.startswith(b"L2"):
                raise ValueError("planted")
            return hardened_parse(blob)

        def invalid_on_reorder(blob: bytes):
            if blob.startswith(b"L2"):
                return ("records", None, ((2,),))
            return hardened_parse(blob)

        lanes = ("records", None, (("L1", "1"), ("L2", "2")))
        for parser in (crash_on_reorder, invalid_on_reorder):
            report = check_permutation(parser, lanes, full=False)
            self.assertEqual(report.violations, 1)
            self.assertEqual(report.notes, ["permutation:1,0"])

    def test_full_permutation_is_bounded(self):
        rows = tuple((f"L{index}", str(index)) for index in range(9))
        calls = []

        def counting(blob: bytes):
            calls.append(blob)
            return hardened_parse(blob)

        report = check_permutation(counting, ("records", None, rows), full=True)
        self.assertEqual(report.violations, 0)
        self.assertEqual(len(calls), 9)
        five = tuple((f"L{index}", str(index)) for index in range(5))
        calls.clear()
        check_permutation(counting, ("records", None, five), full=True)
        self.assertEqual(len(calls), 119)


if __name__ == "__main__":
    unittest.main()
