import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import helpers  # noqa: F401

from slip_lab.__main__ import check_exit
from slip_lab.checkrun import run_check
from slip_lab.dialect import canonical
from slip_lab.hardened import parse as hardened_parse
from slip_lab.legacy import parse as legacy_parse
from slip_lab.model import PRIORITY
from slip_lab.paths import CORPUS
from slip_lab.stock import SPECS, publish


class CorpusTests(unittest.TestCase):
    def test_stock_gate_and_branches(self):
        before = {spec["rel"]: (CORPUS / spec["rel"]).read_bytes() for spec in SPECS}
        with redirect_stderr(io.StringIO()):
            report = run_check()
        after = {spec["rel"]: (CORPUS / spec["rel"]).read_bytes() for spec in SPECS}
        self.assertEqual(before, after)
        self.assertEqual(report["gate_problems"], [])
        self.assertEqual(report["outcome_coverage"], 1.0)
        self.assertEqual(report["disposition_coverage"], 1.0)
        self.assertEqual(report["metamorphic_violations"], 0)
        self.assertGreater(report["metamorphic_skips"], 0)
        self.assertEqual(report["crash_by_side"]["legacy"], 2)
        self.assertEqual(report["hang_by_side"], {"legacy": 0, "hardened": 0, "both": 0})
        self.assertEqual(report["untouched_branches"], ["hang_reject"])
        kinds = [case["outcome"] for case in report["cases"]]
        self.assertEqual(kinds, sorted(kinds, key=lambda kind: PRIORITY[kind]))
        ids = [spec["id"] for spec in SPECS if spec["id"].startswith("SLIP-")]
        self.assertEqual(ids, [f"SLIP-00{index}" for index in range(1, 8)])

    def test_published_corpus_matches_a_fresh_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            fresh = Path(directory)
            publish(fresh)
            for spec in SPECS:
                self.assertEqual((fresh / spec["rel"]).read_bytes(), (CORPUS / spec["rel"]).read_bytes(), spec["rel"])
            self.assertEqual(
                (fresh / "ledger.jsonl").read_bytes(),
                (CORPUS / "ledger.jsonl").read_bytes(),
            )

    def test_drift_fails_the_check_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "corpus"
            shutil.copytree(CORPUS, copy)
            (copy / "fixed" / "middle_empty.slip").write_bytes(b"A|B\n")
            with redirect_stderr(io.StringIO()):
                drifted = run_check(copy)
            self.assertTrue(any("middle_empty" in problem for problem in drifted["gate_problems"]))
            self.assertEqual(check_exit(drifted), 1)

            shutil.rmtree(copy)
            shutil.copytree(CORPUS, copy)
            ledger = copy / "ledger.jsonl"
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            for row in rows:
                if row["id"] == "SLIP-006":
                    row["disposition"] = "accept_spec"
            ledger.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                relabeled = run_check(copy)
            self.assertIn("ledger file does not match the embedded corpus", relabeled["gate_problems"])

            ledger.write_text('{"outcome":"DISAGREE"}\n', encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                rejected = run_check(copy)
            self.assertTrue(any("LedgerError" in problem for problem in rejected["gate_problems"]))

    def test_known_parser_pairs(self):
        self.assertEqual(canonical(legacy_parse(b'""""|1\n')), ("records", None, (("", "1"),)))
        self.assertEqual(canonical(hardened_parse(b'""""|1\n')), ("records", None, (('"', "1"),)))
        self.assertEqual(canonical(legacy_parse(b"A|1|\n")), ("records", None, (("A", "1"),)))
        self.assertEqual(canonical(hardened_parse(b"A|1|\n")), ("records", None, (("A", "1", ""),)))
        self.assertEqual(canonical(legacy_parse(b"#GATE|2\n")), ("records", None, (("#GATE", "2"),)))
        self.assertEqual(canonical(hardened_parse(b"#GATE|2\n")), ("records", None, ()))
        self.assertEqual(canonical(legacy_parse(b"A||B\n")), canonical(hardened_parse(b"A||B\n")))
        with self.assertRaises(RuntimeError):
            legacy_parse(b"A\x00|1\n")
        self.assertEqual(hardened_parse(b"A\x00|1\n"), ("E_NUL",))
        with self.assertRaises(RuntimeError):
            legacy_parse(b"A|1\r\n")
        self.assertEqual(hardened_parse(b"A|1\r\n"), ("E_CARRIAGE",))
        self.assertEqual(hardened_parse(b"__HANG__\n"), ("E_HANG_TOKEN",))


if __name__ == "__main__":
    unittest.main()
