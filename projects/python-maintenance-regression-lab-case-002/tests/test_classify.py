import time
import unittest

import helpers  # noqa: F401

from slip_lab.differential import classify_inprocess, classify_isolated, combine
from slip_lab.model import SideResult
from slip_lab.stock import QUOTE_ROW


class ClassifyTests(unittest.TestCase):
    def test_classify_planted(self):
        agree = classify_isolated(b"L1|1\n", timeout=8)
        self.assertEqual(agree.kind, "AGREE")
        self.assertFalse(agree.truncated)

        disagree = classify_isolated(QUOTE_ROW, timeout=8)
        self.assertEqual(disagree.kind, "DISAGREE")
        self.assertEqual(disagree.side, "both")

        crash_hardened = classify_isolated(
            b"L1|1\n",
            hardened_target="slip_lab.probes:crash_always",
            timeout=8,
        )
        self.assertEqual(crash_hardened.kind, "CRASH")
        self.assertEqual(crash_hardened.side, "hardened")
        self.assertEqual(crash_hardened.hardened.exc_type, "RuntimeError")

        crash_legacy = classify_isolated(b"A\x00|1\n", timeout=8)
        self.assertEqual(crash_legacy.kind, "CRASH")
        self.assertEqual(crash_legacy.side, "legacy")
        self.assertEqual(crash_legacy.legacy.exc_type, "RuntimeError")
        self.assertEqual(crash_legacy.hardened.value, ("E_NUL",))

        started = time.perf_counter()
        hang = classify_isolated(b"__HANG__\n", timeout=0.2)
        elapsed = time.perf_counter() - started
        self.assertEqual(hang.kind, "HANG")
        self.assertEqual(hang.side, "legacy")
        self.assertEqual(hang.timeout_s, 0.2)
        self.assertLess(elapsed, 2.0)

        truncated = classify_isolated(b"L1|1\n", timeout=8, max_output_bytes=8)
        self.assertTrue(truncated.truncated)
        self.assertEqual(truncated.kind, "DISAGREE")
        self.assertIsNone(truncated.legacy.value)
        self.assertIsNone(truncated.hardened.value)

    def test_child_exit_import_failure_and_invalid_value_are_crashes(self):
        exited = classify_isolated(b"L1|1\n", hardened_target="slip_lab.probes:exit_always", timeout=8)
        self.assertEqual((exited.kind, exited.side), ("CRASH", "hardened"))
        self.assertEqual(exited.hardened.exc_type, "ProcessExit")

        missing = classify_isolated(b"L1|1\n", legacy_target="slip_lab.no_such_module:parse", timeout=8)
        self.assertEqual((missing.kind, missing.side), ("CRASH", "legacy"))
        self.assertEqual(missing.legacy.exc_type, "ModuleNotFoundError")

        invalid = classify_isolated(b"L1|1\n", hardened_target="slip_lab.probes:invalid_result", timeout=8)
        self.assertEqual((invalid.kind, invalid.side), ("CRASH", "hardened"))
        self.assertEqual(invalid.hardened.exc_type, "InvalidResult")

        def non_string_field(_blob):
            return ("records", None, ((1, 2),))

        inproc = classify_inprocess(b"x", hardened_fn=non_string_field)
        self.assertEqual((inproc.kind, inproc.side), ("CRASH", "hardened"))
        self.assertEqual(inproc.hardened.exc_type, "InvalidResult")

    def test_compare_ignores_message_text(self):
        def left(_blob):
            return ("E_BARE_QUOTE", "alpha wording")

        def same_code(_blob):
            return ("E_BARE_QUOTE", "beta wording")

        def other_code(_blob):
            return ("E_UNCLOSED_QUOTE", "alpha wording")

        agreed = classify_inprocess(b"x", left, same_code)
        self.assertEqual(agreed.kind, "AGREE")
        differed = classify_inprocess(b"x", left, other_code)
        self.assertEqual(differed.kind, "DISAGREE")

    def test_crash_outranks_hang_and_truncation_skips_equality(self):
        hang = SideResult("hang", None, None, False)
        crash = SideResult("crash", None, "RuntimeError", False)
        out = combine(crash, hang, 0.2)
        self.assertEqual(out.kind, "CRASH")
        self.assertEqual(out.side, "legacy")
        truncated = SideResult("ok", None, None, True)
        full = SideResult("ok", ("records", None, (("A",),)), None, False)
        withheld = combine(truncated, full, 1.0)
        self.assertEqual(withheld.kind, "DISAGREE")
        self.assertTrue(withheld.truncated)
        self.assertIsNone(withheld.legacy.value)


if __name__ == "__main__":
    unittest.main()
