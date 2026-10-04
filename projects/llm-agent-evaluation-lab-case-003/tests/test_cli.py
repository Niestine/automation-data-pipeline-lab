"""CLI report for the synthetic Harborline handbook."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr

import helpers
from rag_eval_lab.__main__ import main

from helpers import EXAMPLES


class CliTests(unittest.TestCase):
    def test_default_report(self) -> None:
        stdout = io.StringIO()
        code = main(["--examples", str(EXAMPLES)], stdout=stdout)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["retriever_leaderboard"]["decisive"])
        self.assertEqual(report["retriever_leaderboard"]["top_config"], "k4-s1-o0-f0.0")
        self.assertEqual(report["retriever_leaderboard"]["operating_point"], "k4-s1-o0-f0.0")
        self.assertFalse(report["retriever_leaderboard"]["overlap_tuned"])
        self.assertFalse(report["generator_comparison"]["decisive"])
        self.assertIsNone(report["generator_comparison"]["winner"])
        rows = report["generator_comparison"]["rows"]
        self.assertEqual([row["generator_id"] for row in rows], ["terse-v1", "trusting-v1"])
        self.assertAlmostEqual(rows[0]["faithfulness"], 1.0)
        self.assertAlmostEqual(rows[1]["faithfulness"], 10 / 11)
        self.assertAlmostEqual(rows[1]["citation_recall"], 10 / 11)
        self.assertEqual(report["confabulation"], {"numerator": 5, "denominator": 11})
        self.assertEqual(report["citation_defects"], {"numerator": 1, "denominator": 8})
        self.assertEqual(report["poison_in_top_k"], 1.0)
        self.assertEqual(report["poison_chunk_count"], 2)
        self.assertAlmostEqual(report["model_label_disagreement"], 0.25)
        self.assertEqual(report["ledger_rows"], 1)
        self.assertEqual(report["retriever_id"], "lexical-v1")
        self.assertIsNone(report["round_trip_retriever_id"])
        self.assertFalse(report["counterfactual"]["detection"])
        self.assertFalse(report["counterfactual"]["correction"])
        self.assertTrue(report["counterfactual"]["guardrail_blocked"])
        self.assertEqual(report["hybrid"]["pooled_best_text_weight"], 0.0)
        self.assertEqual(report["hybrid"]["reasoning_best_text_weight"], 1.0)
        self.assertAlmostEqual(report["sweep_k1_f1"], 1 / 3)
        self.assertAlmostEqual(report["sweep_k4_f1"], 8 / 15)
        self.assertEqual(report["noise_curve"]["points"][-1]["accuracy"], 0.0)
        self.assertEqual(report["noise_curve"]["points"][-1]["claim_recall"], 1.0)
        self.assertGreater(report["events"], 0)
        self.assertFalse(report["dry_run"])

    def test_dry_run_and_approve(self) -> None:
        stdout = io.StringIO()
        code = main(["--examples", str(EXAMPLES), "--dry-run"], stdout=stdout)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["ledger_rows"], 0)
        self.assertEqual(report["confabulation"]["numerator"], 5)
        stdout = io.StringIO()
        code = main(["--examples", str(EXAMPLES), "--approve"], stdout=stdout)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["items"]["q-echo"]["acceptance"], "approved")
        self.assertEqual(report["items"]["q-vendor"]["acceptance"], "reject")
        self.assertEqual(report["items"]["q-gate"]["acceptance"], "auto_accept")

    def test_missing_fixture_exits_with_one_line(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stderr(stderr):
                code = main(["--examples", tmp], stdout=stdout)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        lines = [line for line in stderr.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertIn("missing", lines[0])


if __name__ == "__main__":
    unittest.main()
