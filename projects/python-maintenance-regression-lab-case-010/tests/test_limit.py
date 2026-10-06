"""The recognizer's field limit is not csv.field_size_limit."""

from __future__ import annotations

import csv
import os
import subprocess
import sys
import unittest

import helpers  # noqa: F401
from helpers import SRC

from wharf_sheet.recognize import hardened_parse


class LimitTests(unittest.TestCase):
    def test_exact_limit_passes_and_one_over_fails_after_the_global_limit_is_raised(self):
        previous = csv.field_size_limit(10**7)
        try:
            exact = hardened_parse(b"abcdefgh\r\n", field_limit=8)
            over = hardened_parse(b"abcdefghi\r\n", field_limit=8)
            self.assertEqual(exact.records, (("abcdefgh",),))
            self.assertEqual(over.code, "E-limit")
            self.assertEqual(over.decision_id, "D-limit")
            self.assertEqual(over.byte_offset, 8)
        finally:
            csv.field_size_limit(previous)
        self.assertEqual(csv.field_size_limit(), previous)

    def test_same_boundary_in_a_fresh_process(self):
        script = (
            "import csv\n"
            "from wharf_sheet import hardened_parse\n"
            "fresh = hardened_parse(b'abcdefghi\\r\\n', header='absent', field_limit=8)\n"
            "assert fresh.code == 'E-limit', fresh\n"
            "previous = csv.field_size_limit(10**7)\n"
            "try:\n"
            "    over = hardened_parse(b'abcdefghi\\r\\n', header='absent', field_limit=8)\n"
            "    assert over.code == 'E-limit', over\n"
            "    exact = hardened_parse(b'abcdefgh\\r\\n', header='absent', field_limit=8)\n"
            "    assert exact.records == (('abcdefgh',),), exact\n"
            "finally:\n"
            "    csv.field_size_limit(previous)\n"
            "print('fresh-ok')\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("fresh-ok", completed.stdout)
