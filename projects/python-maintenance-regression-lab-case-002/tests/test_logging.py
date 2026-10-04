import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import helpers  # noqa: F401

from slip_lab.checkrun import run_check
from slip_lab.errors import LogConfigError
from slip_lab.ledger import sha256
from slip_lab.logging_setup import configure, log_case, teardown
from slip_lab.stock import SPECS


class LoggingTests(unittest.TestCase):
    def tearDown(self):
        teardown()

    def test_log_filter(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                path = configure(Path(directory), "t1")
                logger = logging.getLogger("slip_lab.case")
                logger.debug("debug-token")
                logger.info("info-token")
                logger.warning("warning-token")
                logger.error("error-token")
                log_case(
                    logger,
                    case_id="SLIP-001",
                    outcome="DISAGREE",
                    side="both",
                    disposition="bug_legacy",
                    byte_length=4,
                    digest="abc123",
                    parent_ids=["SLIP-000"],
                    truncated=False,
                )
            teardown()
            file_text = path.read_text(encoding="utf-8")
        out = stdout.getvalue()
        err = stderr.getvalue()
        self.assertIn("debug-token", file_text)
        self.assertNotIn("debug-token", out)
        self.assertNotIn("debug-token", err)
        self.assertIn("info-token", out)
        self.assertIn("info-token", file_text)
        self.assertNotIn("info-token", err)
        self.assertIn("warning-token", out)
        self.assertNotIn("warning-token", err)
        self.assertIn("error-token", err)
        self.assertIn("error-token", file_text)
        self.assertNotIn("error-token", out)
        self.assertIn("sha256=abc123", file_text)
        self.assertNotIn("sha256=abc123", out)
        self.assertNotIn("sha256=abc123", err)
        self.assertIn("case=SLIP-001", out)

    def test_stock_run_keeps_payloads_off_info(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                path = configure(Path(directory), "stock")
                run_check()
            teardown()
            file_text = path.read_text(encoding="utf-8")
        out = stdout.getvalue()
        err = stderr.getvalue()
        info_lines = [line for line in out.splitlines() if line.startswith("INFO")]
        self.assertEqual(len(info_lines), len(SPECS))
        self.assertIn("case=SLIP-005 outcome=DISAGREE side=both disposition=bug_new", out)
        for payload in ("#GATE", "A|B", "L1", "@slip", '""'):
            self.assertNotIn(payload, out)
            self.assertNotIn(payload, err)
            self.assertNotIn(payload, file_text)
        self.assertEqual(err.count("exc_type=RuntimeError side=legacy"), 2)
        self.assertNotIn("exc_type=", out)
        quote_row = next(spec for spec in SPECS if spec["id"] == "SLIP-007")
        self.assertIn(f"sha256={sha256(quote_row['blob'])} parents=SLIP-001", file_text)
        self.assertNotIn("sha256=", out)

    def test_refuses_unwritable_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "not-a-directory"
            blocked.write_text("file", encoding="utf-8")
            with self.assertRaises(LogConfigError):
                configure(blocked, "nope")


if __name__ == "__main__":
    unittest.main()
