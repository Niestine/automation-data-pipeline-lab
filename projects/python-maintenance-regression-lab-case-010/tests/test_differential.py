"""Differential witnesses stay classified, minimized, and unwritten."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import corpus_digest

from wharf_sheet.differential import (
    admit,
    campaign,
    examine,
    fold_bytes,
    minimize,
    spelling_notes,
)
from wharf_sheet.model import AdmitError


class DifferentialTests(unittest.TestCase):
    def test_comment_witness_shrinks_without_touching_the_corpus(self):
        before = corpus_digest()
        original = b"# note\r\naaa,bbb\r\nccc,ddd\r\n"

        def signature(candidate: bytes) -> tuple:
            item = examine(candidate, timeout=None)
            return (item.kind, item.decision_id, item.bucket, item.legacy_code, item.hardened_code)

        expected = signature(original)
        shrunk = minimize(original, signature)
        self.assertLess(len(shrunk), len(original))
        self.assertEqual(signature(shrunk), expected)
        self.assertEqual(expected[0], "disagree")
        self.assertEqual(expected[1], "D-comments")
        self.assertIn(expected[2], {"b", "n"})
        self.assertEqual(corpus_digest(), before)

    def test_minimize_runs_until_a_full_cycle_changes_nothing(self):
        # Longer than any fixed round cap the old loop had. Only the "#" matters.
        original = b"x" * 150 + b"#" + b"y" * 150

        def signature(candidate: bytes) -> bool:
            return b"#" in candidate

        self.assertEqual(minimize(original, signature), b"#")

    def test_spelling_notes_cover_separators_and_quotes_only(self):
        with self.assertLogs("wharf_sheet", level="INFO") as captured:
            self.assertEqual(
                spelling_notes(b"a,b\r\nc,d\r\n", b"a,b\nc,d\r\n"),
                ("separator",),
            )
        self.assertEqual(captured.output, ["INFO:wharf_sheet:normalization separator context=pair"])
        self.assertEqual(
            spelling_notes(b"'aaa','bbb'\r\n", b'"aaa","bbb"\r\n'),
            ("quote-style",),
        )
        folded, notes = fold_bytes(b"12345678901234567,ok\r\n")
        self.assertEqual(folded, b"12345678901234567,ok\r\n")
        self.assertEqual(notes, ())

    def test_same_seed_keeps_the_minimized_class_set(self):
        first = campaign(seed=20261006, budget=24, field_limit=8, timeout=None)
        second = campaign(seed=20261006, budget=24, field_limit=8, timeout=None)

        def key(result):
            return sorted((item.decision_id, item.data) for item in result.witnesses)

        self.assertEqual(key(first), key(second))
        self.assertEqual(first.unclassified, 0)
        self.assertEqual(second.unclassified, 0)
        self.assertEqual(first.files_written, 0)
        self.assertEqual(second.files_written, 0)
        self.assertGreater(first.agreements, 0)

    def test_limit_mutant_adds_a_classified_witness(self):
        plain = campaign(seed=20261006, budget=8, field_limit=8, timeout=None)
        mutated = campaign(
            seed=20261006,
            budget=8,
            field_limit=8,
            timeout=None,
            mutant="limit_boundary_swap",
        )
        self.assertNotIn("D-limit", plain.classes)
        self.assertIn("D-limit", mutated.classes)
        self.assertEqual(plain.unclassified, 0)
        self.assertEqual(mutated.unclassified, 0)
        self.assertEqual(plain.files_written, 0)
        self.assertEqual(mutated.files_written, 0)

    def test_admit_refuses_an_unclassified_witness(self):
        before = corpus_digest()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with self.assertRaises(AdmitError):
                admit(directory, "n_demo", b"a\r\n", {"decision_id": "D-empty"})
            self.assertEqual(list(directory.iterdir()), [])
            admit(
                directory,
                "n_demo",
                b"",
                {"bucket": "n", "decision_id": "D-empty"},
            )
            self.assertEqual(
                sorted(path.name for path in directory.iterdir()),
                ["n_demo.bin", "n_demo.json"],
            )
            sidecar = {"bucket": "n", "decision_id": "D-empty"}
            with self.assertRaises(AdmitError):
                admit(directory, "n_demo", b"other", sidecar)
            for bad in ("nul", "../n_escape", "y_demo", "n_Demo", "n_demo.txt"):
                with self.subTest(name=bad), self.assertRaises(AdmitError):
                    admit(directory, bad, b"", sidecar)
            self.assertEqual((directory / "n_demo.bin").read_bytes(), b"")
            self.assertEqual(len(list(directory.iterdir())), 2)
            self.assertFalse((directory.parent / "n_escape.bin").exists())
        self.assertEqual(corpus_digest(), before)

    def test_a_disagreeing_witness_logs_its_normalization(self):
        with self.assertLogs("wharf_sheet", level="INFO") as captured:
            witness = examine(b"p,q\nr,s\r\n", timeout=None)
        self.assertEqual((witness.kind, witness.decision_id), ("disagree", "D-crlf"))
        self.assertEqual(witness.normalizations, ("separator",))
        self.assertIn("INFO:wharf_sheet:normalization separator context=witness", captured.output)

    def test_a_sleeping_parser_is_a_timeout_witness(self):
        def sleepy() -> None:
            time.sleep(30)

        started = time.monotonic()
        witness = examine(b"a,b\r\n", timeout=0.05, legacy_runner=sleepy)
        elapsed = time.monotonic() - started
        self.assertEqual(witness.kind, "timeout")
        self.assertLess(elapsed, 2.0)
