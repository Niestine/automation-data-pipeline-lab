"""Kill rate and statement coverage are separate scores."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401
from helpers import CORPUS

from wharf_sheet.corpus import call_case, load_corpus, outcome_matches, strict_expectation
from wharf_sheet.mutants import (
    EQUIVALENT,
    MUTANTS,
    PROFILE,
    kill_rate,
    kills,
    statement_coverage,
    survivor_names,
)

# One load-bearing corpus file per mutant.
KILLERS = {
    "field_count_swap": "b_record_ragged.bin",
    "limit_boundary_swap": "y_limit_exact.bin",
    "drop_unclosed": "n_quote_unclosed.bin",
    "drop_doubled_quote": "y_quote_doubled.bin",
    "semicolon_comma": "y_field_semicolon.bin",
    "drop_ascii_restore": "b_decode_efbb_comma.bin",
}


class MutantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_corpus(CORPUS)

    def test_every_unmarked_mutant_is_killed(self):
        self.assertEqual(len(set(MUTANTS)), len(MUTANTS))
        self.assertEqual(set(PROFILE), set(MUTANTS))
        for name, reason in EQUIVALENT.items():
            self.assertIn(name, MUTANTS)
            self.assertTrue(reason.strip())
        self.assertEqual(survivor_names(self.cases), [])
        self.assertEqual(kill_rate(self.cases), 1.0)

    def test_each_mutant_is_killed_by_its_named_file(self):
        by_name = {case.name: case for case in self.cases}
        for mutant, filename in KILLERS.items():
            with self.subTest(mutant=mutant):
                self.assertTrue(kills(by_name[filename], mutant))
        self.assertEqual(set(KILLERS), set(MUTANTS))

    def test_unmutated_parsers_pass_every_case(self):
        # A kill only means something if the clean parser satisfies the same oracle.
        for case in self.cases:
            with self.subTest(case=case.name):
                self.assertTrue(outcome_matches(call_case(case, "hardened", None), strict_expectation(case)))

    def test_removing_the_unclosed_quote_file_lowers_the_kill_rate(self):
        full = kill_rate(self.cases)
        reduced = kill_rate(self.cases, frozenset({"n_quote_unclosed.bin"}))
        self.assertLess(reduced, full)
        self.assertIn("drop_unclosed", survivor_names(self.cases, frozenset({"n_quote_unclosed.bin"})))

    def test_statement_coverage_is_reported_apart_from_the_kill_rate(self):
        cases = self.cases

        def drive():
            for case in cases:
                call_case(case, "hardened", None)
                call_case(case, "legacy", None)

        report = {
            "kill_rate": kill_rate(cases),
            "statement_coverage": statement_coverage(drive),
        }
        self.assertEqual(report["kill_rate"], 1.0)
        self.assertGreater(report["statement_coverage"], 0.5)
        self.assertLessEqual(report["statement_coverage"], 1.0)
        self.assertEqual(set(report), {"kill_rate", "statement_coverage"})
