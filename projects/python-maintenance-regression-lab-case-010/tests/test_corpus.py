"""Corpus gates: buckets, decisions, strict outcomes, legacy breaks, handlers."""

from __future__ import annotations

import copy
import re
import unittest

import helpers  # noqa: F401
from helpers import CORPUS, LIBRARY

from wharf_sheet.corpus import Case, case_problems, catalog_problems, load_corpus
from wharf_sheet.model import ParseFailure, ParseSuccess
from wharf_sheet.recognize import hardened_parse, legacy_parse


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_corpus(CORPUS)

    def test_catalog_covers_every_decision_without_prefix_duplicates(self):
        self.assertEqual(catalog_problems(self.cases), [])
        buckets = {case.meta["bucket"] for case in self.cases}
        self.assertEqual(buckets, {"y", "n", "i", "c", "b"})

    def test_each_sidecar_matches_its_parser(self):
        problems = [problem for case in self.cases for problem in case_problems(case)]
        self.assertEqual(problems, [])

    def test_handler_runs_only_after_strict_acceptance(self):
        for case in self.cases:
            seen = []
            result = hardened_parse(
                case.data,
                header=case.meta["header"],
                charset=case.meta.get("charset", "utf-8"),
                field_limit=int(case.meta.get("field_limit", 4096)),
                handler=seen.append,
            )
            if isinstance(result, ParseSuccess):
                self.assertEqual(seen, [result.records])
                self.assertIsInstance(seen[0], tuple)
            else:
                self.assertIsInstance(result, ParseFailure)
                self.assertEqual(seen, [])

    def test_legacy_break_still_calls_the_handler_with_records(self):
        case = next(item for item in self.cases if item.name == "b_comment_skipped.bin")
        seen = []
        result = legacy_parse(case.data, header="absent", handler=seen.append)
        self.assertIsInstance(result, ParseSuccess)
        self.assertEqual(seen, [result.records])
        self.assertEqual(result.records, (("x", "y"),))

    def test_every_parser_error_code_has_a_strict_corpus_outcome(self):
        source = "\n".join(
            (LIBRARY / name).read_text(encoding="utf-8") for name in ("recognize.py", "decode.py")
        )
        raised = set(re.findall(r'"(E-[a-z0-9-]+)"', source))
        stored = set()
        for case in self.cases:
            block = case.meta["hardened"] if case.meta["bucket"] == "b" else case.meta
            if block.get("error_code"):
                stored.add(block["error_code"])
        self.assertGreaterEqual(len(raised), 15)
        self.assertEqual(sorted(raised - stored), [])

    def _with(self, name: str, **changes) -> list[Case]:
        cases = []
        for case in self.cases:
            if case.name == name:
                meta = copy.deepcopy(case.meta)
                meta.update(changes.pop("meta", {}))
                case = Case(case.name, changes.pop("data", case.data), meta)
            cases.append(case)
        return cases

    def test_catalog_gates_fire_on_a_broken_corpus(self):
        drifted = self._with("b_comment_skipped.bin", meta={"removal_condition": "never"})
        self.assertIn(
            "b_comment_skipped.bin removal condition drifted from D-comments",
            catalog_problems(drifted),
        )

        uncovered = [case for case in self.cases if case.meta["decision_id"] != "D-digits"]
        self.assertIn("D-digits has no corpus file", catalog_problems(uncovered))

        bad_event = self._with(
            "b_comment_skipped.bin",
            meta={"legacy": dict(self._meta("b_comment_skipped.bin")["legacy"], events=[
                {"decision_id": "D-made-up", "record_index": 0, "field_index": 0, "byte_offset": 0}
            ])},
        )
        self.assertTrue(any("unknown decision 'D-made-up'" in item for item in catalog_problems(bad_event)))

    def test_prefix_duplicates_are_found_among_every_file_with_the_signature(self):
        # Three files share one signature. The prefix pair is the second and third,
        # so a check against only the first-seen file would miss it.
        # header "present" gives a signature no real corpus file has.
        meta = dict(self._meta("n_comment_hash.bin"), header="present")
        extra = [
            Case("n_comment_zz_a.bin", b"#x\r\n", dict(meta)),
            Case("n_comment_zz_b.bin", b"#ab\r\n", dict(meta)),
            Case("n_comment_zz_c.bin", b"#ab\r\nc\r\n", dict(meta)),
        ]
        problems = catalog_problems(self.cases + extra)
        self.assertEqual(
            [item for item in problems if "zz" in item],
            ["n_comment_zz_b.bin repeats the signature of n_comment_zz_c.bin as a proper prefix"],
        )

    def test_case_check_rejects_a_sidecar_the_parser_does_not_match(self):
        wrong = self._with("y_field_spaces.bin", meta={"records": [["a", "b"]]})
        case = next(item for item in wrong if item.name == "y_field_spaces.bin")
        self.assertNotEqual(case_problems(case), [])

    def _meta(self, name: str) -> dict:
        return next(case.meta for case in self.cases if case.name == name)
