"""The project command prints oracle counts and stays offline."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import unittest

import helpers as _path  # noqa: F401  (puts src/ on sys.path)

from prefixlab.__main__ import main
from prefixlab.audit import collect


class CliTests(unittest.TestCase):
    def tearDown(self):
        log = logging.getLogger("prefixlab")
        for handler in list(log.handlers):
            log.removeHandler(handler)

    def test_metrics_match_the_oracles(self):
        report = collect()
        self.assertEqual(report["recovery_mismatches"], 0)
        self.assertEqual(report["double_restart_extra"], 0)
        self.assertEqual(report["safe_corrupt"], 0)
        self.assertEqual(report["reorder_outcome"], "corrupt")
        self.assertGreaterEqual(report["relaxed_corrupt"], 1)
        self.assertEqual(report["nolock_sum"], 8)
        self.assertEqual(report["exclusive_sum"], 5)
        self.assertIsNone(report["anomalies"]["serial"])
        self.assertEqual(report["anomalies"]["g0"], "G0")
        self.assertEqual(report["anomalies"]["g1a"], "G1a")
        self.assertIsNone(report["anomalies"]["unknown"])

    def test_main_writes_json_and_rejects_an_unknown_command(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = main(["metrics"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["safe_corrupt"], 0)
        self.assertEqual(payload["exclusive_sum"], 5)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["nope"]), 2)
        demo = io.StringIO()
        with contextlib.redirect_stdout(demo), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["demo"]), 0)
        self.assertIn("recovery_mismatches=0", demo.getvalue())
        self.assertIn("safe_corrupt=0", demo.getvalue())


if __name__ == "__main__":
    unittest.main()
