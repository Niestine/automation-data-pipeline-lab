"""CLI smoke tests. Output is ASCII JSON and stays inside a temp directory."""

from __future__ import annotations

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import helpers  # noqa: F401
from helpers import EXAMPLES, GOLDEN_SHA256, LabTest

from netunicode_lab.__main__ import main


def run_main(argv):
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class CliTests(LabTest):
    def test_ingest_e_acute_and_mixed_newlines(self):
        code, stdout, stderr = run_main(["ingest", str(EXAMPLES / "e_acute.bin")])
        self.assertEqual(code, 0, stderr)
        body = json.loads(stdout)
        self.assertEqual(body["logical_text"], "\u00e9")
        self.assertEqual(body["octets"], 2)

        code, stdout, stderr = run_main(["ingest", str(EXAMPLES / "foreign_mixed.bin")])
        self.assertEqual(code, 0, stderr)
        body = json.loads(stdout)
        self.assertEqual(body["logical_text"], "a\nb\nc\nd")

    def test_ingest_overlong_nul_is_an_error_record(self):
        code, stdout, stderr = run_main(["ingest", str(EXAMPLES / "overlong_nul.bin")])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        body = json.loads(stderr)
        self.assertEqual(body["error"], "StrictUtf8Error")
        self.assertIn("strict-utf8", body["message"])
        self.assertIn("0xc0", body["message"])

    def test_log_signatures_prints_the_boundary_line_without_payload(self):
        code, stdout, stderr = run_main(
            ["--log-signatures", "ingest", str(EXAMPLES / "overlong_nul.bin")]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        log_line, record = stderr.splitlines()
        self.assertEqual(
            log_line,
            "WARNING netunicode_lab boundary=foreign-ingest codec=strict-utf8 byte=0xc0",
        )
        self.assertEqual(json.loads(record)["error"], "StrictUtf8Error")
        # The handler is removed after the call, so library use stays quiet.
        code, _, stderr = run_main(["ingest", str(EXAMPLES / "overlong_nul.bin")])
        self.assertEqual(code, 1)
        self.assertEqual(len(stderr.splitlines()), 1)

    def test_rows_file_that_is_not_utf8_is_an_error_record(self):
        rows = self.root / "rows.json"
        rows.write_bytes(b'[["caf\xe9"]]')
        destination = self.root / "out.csv"
        code, stdout, stderr = run_main(["emit", str(destination), "--rows", str(rows)])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"], "UnicodeDecodeError")
        self.assertFalse(destination.exists())

    def test_emit_and_check_round_trip_the_golden_digest(self):
        destination = self.root / "rows.csv"
        code, stdout, stderr = run_main(
            ["emit", str(destination), "--rows", str(EXAMPLES / "rows.json")]
        )
        self.assertEqual(code, 0, stderr)
        body = json.loads(stdout)
        self.assertEqual(body["sha256"], GOLDEN_SHA256)
        self.assertEqual(body["octets"], 15)
        code, stdout, stderr = run_main(["check", str(destination)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["sha256"], GOLDEN_SHA256)

    def test_diagnose_command_counts_a_short_field(self):
        code, stdout, stderr = run_main(["diagnose", "\u00e9$"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {"badness": 1})

    def test_check_rejects_a_doubled_newline_file(self):
        path = self.root / "broken.csv"
        path.write_bytes(b"a\r\r\nb\r\n")
        code, stdout, stderr = run_main(["check", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        body = json.loads(stderr)
        self.assertEqual(body["error"], "ProfileError")
        self.assertIn("0x0d", body["message"])


if __name__ == "__main__":
    unittest.main()
