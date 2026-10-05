"""The offline commands print the same verdicts the tests lock."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import helpers  # noqa: F401

from contract_lab.__main__ import main
from contract_lab.compliance import cited_figures
from contract_lab.logsetup import get_logger
from contract_lab.rules import RULES
from helpers import EXAMPLES


class CommandTests(unittest.TestCase):
    def tearDown(self) -> None:
        # ``call`` attaches a stderr handler; keep it out of later test modules.
        logger = get_logger()
        for handler in list(logger.handlers):
            if getattr(handler, "contract_lab_cli", False):
                logger.removeHandler(handler)

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_metrics_prints_separate_counts(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["metrics"]), 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["best_case_consistent_apis"], cited_figures()["best_case_consistent_apis"])
        self.assertEqual(payload["seeded_rule_count"], len(RULES))
        self.assertTrue(payload["api_count_sum_is_not_studied_population"])

    def test_classify_prints_the_reader_split(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["classify", str(EXAMPLES / "histories.json")]), 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["compliance"]["strict"], "leaking")
        self.assertEqual(payload["compliance"]["tolerant"], "compatible_only")
        self.assertTrue(payload["protocol_ok"])

    def test_call_prints_the_description_warning(self) -> None:
        code, out, err = self._run("call", str(EXAMPLES / "call_description.json"))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["business"], {"gauge_id": "g-1", "height_mm": 1200})
        self.assertEqual(payload["events"][0]["channel"], "description")
        self.assertEqual(payload["events"][0]["replacement"], "/stations/{station_id}")
        self.assertEqual(payload["phase"], "not_deprecated")
        self.assertIn("WARNING contract_lab deprecation_warning GET /gauges/{gauge_id}", err)
        self.assertIn('"replacement": "/stations/{station_id}"', err)

    def test_failures_return_codes_instead_of_tracebacks(self) -> None:
        code, out, err = self._run("classify", str(EXAMPLES / "missing.json"))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("FileNotFoundError", err)
        fixture = json.loads((EXAMPLES / "call_description.json").read_text(encoding="utf-8"))
        fixture["reader"] = "strict"
        fixture["response"]["body"]["spare_flag"] = True
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "strict_call.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            code, out, err = self._run("call", str(path))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("contract failure: SchemaRejected: /spare_flag", err)
        self.assertEqual(self._run("unknown")[0], 2)
