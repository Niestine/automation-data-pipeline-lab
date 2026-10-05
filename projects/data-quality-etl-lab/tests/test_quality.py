"""Injector, detector metrics, duplicate decisions, and JCS vectors.

Scores against the synthetic cell oracle measure generator soundness.
They are not an estimate of in-the-wild recall.
"""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from decimal import Decimal

import support
from harbor_ledger.dedup import (
    candidate_pairs,
    cluster_links,
    decide_pair,
    match_rows,
    passes_separately,
    window_pairs,
)
from harbor_ledger.dedup import PairDecision
from harbor_ledger.detectors import finding_signature, min_k_filter, modified_z_flags, run_detectors
from harbor_ledger.errors import JCSError
from harbor_ledger.injector import run_injection
from harbor_ledger.jcs import assert_ascii_keys, canonicalize, canonicalize_items, canonicalize_json, utf16_key
from harbor_ledger.metrics import score_slices, suggest_outlier_threshold
from harbor_ledger.models import ViewRow
from harbor_ledger.rules import repairability
from harbor_ledger.schema import schema_from_dict, standardize_text
from harbor_ledger.similarity import cosine, edit_similarity, jaro_winkler, monge_elkan, qgrams, token_qgrams


def _view(row_id: str, source: int, values: dict) -> ViewRow:
    return ViewRow(
        row_id=row_id,
        source_row=source,
        output_row=source,
        values=dict(values),
        raw={key: "" if value is None else str(value) for key, value in values.items()},
        match={key: None if value is None else str(value) for key, value in values.items()},
    )


def _payroll_schema():
    return schema_from_dict(
        {
            "name": "payroll-lab",
            "primaryKey": ["emp_id"],
            "missingValues": [""],
            "dialect": {"header": False},
            "fields": [
                {"name": "emp_id", "type": "string", "constraints": {"required": True}},
                {"name": "office", "type": "string", "constraints": {"required": True}},
                {"name": "dept", "type": "string", "constraints": {"required": True}},
                {"name": "supplier_code", "type": "string"},
                {
                    "name": "salary",
                    "type": "number",
                    "constraints": {"minimum": "0", "maximum": "100"},
                },
            ],
            "rules": [
                {
                    "id": "office_dept",
                    "kind": "fd",
                    "determinant": "office",
                    "dependent": "dept",
                },
                {
                    "id": "east_knit",
                    "kind": "constant",
                    "when": {"field": "supplier_code", "equals": "EAST"},
                    "then": {"field": "dept", "equals": "knit"},
                },
            ],
        }
    )


def _employee(row_id: str, source: int, dept: str, office: str = "HQ", supplier: str = "NORTH", salary: str = "10") -> ViewRow:
    return _view(
        row_id,
        source,
        {
            "emp_id": row_id,
            "office": office,
            "dept": dept,
            "supplier_code": supplier,
            "salary": Decimal(salary),
        },
    )


class RepairabilityTests(unittest.TestCase):
    def test_fd_bag_constant_range_and_the_max_across_rules(self) -> None:
        schema = _payroll_schema()
        depts = ["Staff", "Staff", "Sales", "Mktg", "Mktg"]
        rows = [_employee(f"e{index}", index, dept) for index, dept in enumerate(depts, start=1)]
        detected, exactly_one, score, rules = repairability(rows, schema, "e3", "dept", "Sales")
        self.assertTrue(detected)
        self.assertTrue(exactly_one)
        self.assertEqual(score, "0.2")
        self.assertEqual(rules, ["office_dept"])

        constant_row = [_employee("e1", 1, "shirting", supplier="EAST")]
        detected, exactly_one, score, rules = repairability(constant_row, schema, "e1", "dept", "knit")
        self.assertTrue(detected)
        self.assertTrue(exactly_one)
        self.assertEqual(score, "1")
        self.assertEqual(rules, ["east_knit"])

        ranged = [_employee("e1", 1, "Sales", salary="500")]
        detected, exactly_one, score, rules = repairability(ranged, schema, "e1", "salary", Decimal("10"))
        self.assertTrue(detected)
        self.assertEqual(score, "0")
        self.assertEqual(rules, ["range:salary"])

        rows[2].values["supplier_code"] = "EAST"
        detected, exactly_one, score, rules = repairability(rows, schema, "e3", "dept", "Sales")
        self.assertTrue(detected)
        self.assertFalse(exactly_one)
        self.assertEqual(score, "1")
        self.assertEqual(rules, ["east_knit", "office_dept"])

        quiet = [_employee("e1", 1, "Sales")]
        self.assertEqual(repairability(quiet, schema, "e1", "supplier_code", "NORTH"), (False, False, None, []))


class InjectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = _payroll_schema()

    def test_preservation_immutable_cells_and_one_change_per_cell(self) -> None:
        rows = [
            _employee("r1", 1, "Sales", office="A"),
            _employee("r2", 2, "Sales", office="A"),
            _employee("r3", 3, "Sales", office="B"),
        ]
        snapshot = [row.copy() for row in rows]
        result = run_injection(
            rows,
            self.schema,
            {
                "detectable_quota": 2,
                "immutable": ["supplier_code"],
                "proposals": [
                    {"row_id": "r1", "column": "supplier_code", "value": "EAST", "class": "constraint_break"},
                    {"row_id": "r2", "column": "dept", "value": "Staff", "class": "constraint_break"},
                    {"row_id": "r1", "column": "dept", "value": "Staff", "class": "constraint_break"},
                    {"row_id": "r3", "column": "salary", "value": Decimal("500"), "class": "outlier"},
                    {"row_id": "r3", "column": "salary", "value": Decimal("40"), "class": "typo"},
                ],
            },
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.shortfall, 0)
        self.assertEqual(result.rows[0].values["dept"], "Sales")
        self.assertEqual(result.rows[0].values["supplier_code"], "NORTH")
        self.assertEqual(result.rows[1].values["dept"], "Staff")
        self.assertEqual(result.rows[2].values["salary"], Decimal("500"))
        placed = {(item["row_id"], item["column"]) for item in result.oracle}
        self.assertEqual(placed, {("r2", "dept"), ("r3", "salary")})
        by_cell = {(item["row_id"], item["column"]): item for item in result.oracle}
        self.assertTrue(by_cell[("r2", "dept")]["detectable"])
        self.assertEqual(by_cell[("r2", "dept")]["exactly_one"], True)
        self.assertTrue(by_cell[("r3", "salary")]["detectable"])
        self.assertEqual(by_cell[("r3", "salary")]["repairability"], "0")
        self.assertEqual(rows[0].values["dept"], snapshot[0].values["dept"])
        self.assertEqual(result.rows[1].match["dept"], "staff")

    def test_in_range_salary_is_undetectable(self) -> None:
        rows = [_employee("r1", 1, "Sales")]
        result = run_injection(
            rows,
            self.schema,
            {
                "detectable_quota": 0,
                "undetectable_quota": 1,
                "proposals": [
                    {"row_id": "r1", "column": "salary", "value": Decimal("50"), "class": "typo"},
                ],
            },
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(result.oracle), 1)
        self.assertFalse(result.oracle[0]["detectable"])
        self.assertIsNone(result.oracle[0]["repairability"])
        self.assertEqual(result.rows[0].values["salary"], Decimal("50"))

    def test_shortfall_exits_zero_and_a_dirty_fixture_exits_two(self) -> None:
        rows = [_employee("r1", 1, "Sales", office="A"), _employee("r2", 2, "Sales", office="A")]
        short = run_injection(
            rows,
            self.schema,
            {
                "detectable_quota": 5,
                "proposals": [
                    {"row_id": "r2", "column": "dept", "value": "Staff", "class": "constraint_break"},
                ],
            },
        )
        self.assertEqual(short.exit_code, 0)
        self.assertEqual(short.shortfall, 4)
        dirty = [_employee("r1", 1, "Sales", office="HQ"), _employee("r2", 2, "Staff", office="HQ")]
        failed = run_injection(dirty, self.schema, {"detectable_quota": 1, "proposals": []})
        self.assertEqual(failed.exit_code, 2)
        self.assertEqual(failed.oracle, [])
        self.assertEqual(failed.shortfall, 0)
        self.assertIs(failed.rows, dirty)

    def test_seeded_random_injection_is_reproducible_and_skips_the_key(self) -> None:
        schema = schema_from_dict(
            {
                "name": "random-lab",
                "primaryKey": ["emp_id"],
                "fields": [
                    {"name": "emp_id", "type": "string", "constraints": {"required": True}},
                    {"name": "label", "type": "string", "constraints": {"required": True}},
                    {
                        "name": "salary",
                        "type": "number",
                        "constraints": {"minimum": "0", "maximum": "100"},
                    },
                ],
            }
        )
        rows = [
            _view(f"e{index}", index, {"emp_id": f"e{index}", "label": f"item{index}", "salary": Decimal("20")})
            for index in range(1, 5)
        ]
        config = {"detectable_quota": 3, "seed": 4180, "classes": ["typo", "constraint_break", "outlier", "missing"]}
        first = run_injection(rows, schema, config)
        second = run_injection(rows, schema, config)
        self.assertEqual(first.exit_code, 0)
        self.assertEqual(first.oracle, second.oracle)
        self.assertEqual(first.shortfall, second.shortfall)
        self.assertTrue(first.oracle)
        self.assertTrue(all(item["column"] != "emp_id" for item in first.oracle))
        self.assertEqual(len({(item["row_id"], item["column"]) for item in first.oracle}), len(first.oracle))


class DetectorMetricTests(unittest.TestCase):
    def _price_rows(self) -> tuple[object, list[ViewRow], dict]:
        schema = schema_from_dict(
            {
                "name": "price-lab",
                "primaryKey": ["sku"],
                "fields": [
                    {"name": "sku", "type": "string", "constraints": {"required": True}},
                    {"name": "style", "type": "string", "constraints": {"required": True, "pattern": "[A-Z0-9]+"}},
                    {
                        "name": "price",
                        "type": "number",
                        "constraints": {"minimum": "0", "maximum": "100"},
                    },
                ],
            }
        )
        prices = ["10", "12", "11", "13", "10", "500"]
        rows = []
        for index, price in enumerate(prices, start=1):
            style = "bad!" if index == 1 else "ABC"
            rows.append(
                _view(
                    f"r{index}",
                    index,
                    {"sku": f"r{index}", "style": style, "price": Decimal(price)},
                )
            )
        profile = {"outlier": {"method": "modified_z", "threshold": "3.5", "minimum_rows": 5}}
        return schema, rows, profile

    def test_modified_z_threshold_is_frozen_separately_from_the_suggestion(self) -> None:
        values = [Decimal(item) for item in ["10", "12", "11", "13", "10", "500"]]
        strict = modified_z_flags(values, Decimal("3.5"))
        loose = modified_z_flags(values, Decimal("0.1"))
        self.assertEqual(strict, [False, False, False, False, False, True])
        self.assertEqual(loose, [True] * 6)
        suggested = suggest_outlier_threshold(values)
        self.assertNotEqual(modified_z_flags(values, suggested), strict)
        self.assertEqual(modified_z_flags(values, Decimal("3.5")), strict)

    def test_union_ignores_order_and_min_k_is_not_the_published_set(self) -> None:
        schema, rows, profile = self._price_rows()
        forward = run_detectors(rows, schema, profile, order=["pattern_type", "constraint", "outlier"])
        backward = run_detectors(rows, schema, profile, order=["outlier", "constraint", "pattern_type"])
        self.assertEqual(
            {finding_signature(item) for item in forward},
            {finding_signature(item) for item in backward},
        )
        published = {finding_signature(item) for item in forward}
        pattern_hits = [item for item in forward if item["detector"] == "pattern_type"]
        self.assertTrue(pattern_hits)
        filtered = min_k_filter(forward, 2)
        self.assertTrue(all(item["column"] == "price" for item in filtered))
        self.assertLess(len(filtered), len(forward))
        self.assertTrue(published.issuperset(finding_signature(item) for item in filtered))
        self.assertTrue(any(item["detector"] == "pattern_type" for item in forward))
        self.assertFalse(any(item["detector"] == "pattern_type" for item in filtered))

    def test_repair_between_turns_a_pattern_failure_into_a_missing_value(self) -> None:
        schema, rows, profile = self._price_rows()
        sealed = run_detectors(rows, schema, profile, order=["pattern_type", "constraint"], repair_between=False)
        repaired = run_detectors(rows, schema, profile, order=["pattern_type", "constraint"], repair_between=True)
        sealed_classes = {(item["record_key"], item["column"], item["class"]) for item in sealed}
        repaired_classes = {(item["record_key"], item["column"], item["class"]) for item in repaired}
        self.assertIn(("r1", "style", "bogus"), sealed_classes)
        self.assertNotIn(("r1", "style", "missing"), sealed_classes)
        self.assertIn(("r1", "style", "bogus"), repaired_classes)
        self.assertIn(("r1", "style", "missing"), repaired_classes)
        self.assertEqual(rows[0].values["style"], "bad!")

    def test_per_class_scores_have_no_headline_f_and_upper_recall_covers_misses(self) -> None:
        schema, rows, profile = self._price_rows()
        findings = run_detectors(rows, schema, profile, order=["pattern_type", "constraint", "outlier"])
        oracle = [
            {"row_id": "r6", "column": "price", "class": "outlier", "slice": "eval"},
            {"row_id": "r1", "column": "style", "class": "missing", "slice": "train"},
        ]
        slices = {f"r{index}": "eval" for index in range(1, 7)}
        slices["r1"] = "train"
        scored = score_slices(findings, oracle, slices)
        self.assertNotIn("headline_f", scored)
        self.assertNotIn("headline_f", scored["evaluation"])
        evaluation = scored["evaluation"]
        self.assertEqual(evaluation["outlier"]["outlier"]["f"], "1.000000")
        self.assertEqual(evaluation["outlier"]["outlier"]["precision"], "1.000000")
        self.assertEqual(evaluation["outlier"]["outlier"]["recall"], "1.000000")
        self.assertEqual(evaluation["constraint"]["outlier"]["f"], "0.000000")
        self.assertEqual(evaluation["constraint"]["outlier"]["upper_recall"], "0.000000")
        training = scored["training"]["constraint"]["missing"]
        self.assertEqual(training["recall"], "0.000000")
        self.assertEqual(training["upper_recall"], "1.000000")
        for slice_report in scored.values():
            for detector_report in slice_report.values():
                for cell in detector_report.values():
                    self.assertGreaterEqual(Decimal(cell["upper_recall"]), Decimal(cell["recall"]))

    def test_scores_count_cells_not_repeated_findings(self) -> None:
        base = {"detector": "constraint", "class": "constraint_break", "source_row": 1, "output_row": 1, "detail": ""}
        findings = [
            dict(base, record_key="r1", column="price", rule="range:price"),
            dict(base, record_key="r1", column="price", rule="enum:price"),
            dict(base, record_key="r2", column="price", rule="range:price"),
        ]
        oracle = [{"row_id": "r1", "column": "price", "class": "constraint_break"}]
        scored = score_slices(findings, oracle, {"r1": "eval", "r2": "eval"})
        cell = scored["evaluation"]["constraint"]["constraint_break"]
        self.assertEqual((cell["tp"], cell["fp"], cell["fn"]), (1, 1, 0))
        self.assertEqual(cell["precision"], "0.500000")
        self.assertEqual(cell["recall"], "1.000000")


class DedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = json.loads((support.EXAMPLES / "profile.json").read_text(encoding="utf-8"))

    def test_window_and_a_second_blocking_key(self) -> None:
        self.assertEqual(
            window_pairs(["a", "b", "c", "d"], 2),
            [("a", "b"), ("b", "c"), ("c", "d")],
        )
        wide = window_pairs(["a", "b", "c", "d"], 3)
        self.assertIn(("a", "c"), wide)
        self.assertNotIn(("a", "d"), wide)
        rows = [
            _match_row("A", 1, "aaa", "navy coat"),
            _match_row("M", 2, "mmm", "other"),
            _match_row("Z", 3, "zzz", "navy tee"),
        ]
        style_pairs = passes_separately(rows, "style_code", 2)
        title_pairs = passes_separately(rows, "title_prefix", 2)
        self.assertNotIn(("A", "Z"), style_pairs)
        self.assertIn(("A", "Z"), title_pairs)

    def test_clerical_band_does_not_bridge_clusters(self) -> None:
        decisions = [
            PairDecision("A", "B", "link", Decimal("3"), (), 1, 2),
            PairDecision("B", "C", "possible", Decimal("1"), (), 2, 3),
            PairDecision("C", "D", "link", Decimal("3"), (), 3, 4),
        ]
        self.assertEqual(cluster_links(decisions, ["A", "B", "C", "D"]), [["A", "B"], ["C", "D"]])
        chained = [
            PairDecision("A", "B", "link", Decimal("3"), (), 1, 2),
            PairDecision("B", "C", "link", Decimal("3"), (), 2, 3),
        ]
        self.assertEqual(cluster_links(chained, ["A", "B", "C", "D"]), [["A", "B", "C"]])

    def test_window_only_compares_rows_within_w_minus_one_after_the_sort(self) -> None:
        rows = [
            _match_row("A", 1, "AA1", "alpha"),
            _match_row("B", 2, "AA2", "bravo"),
            _match_row("C", 3, "AA3", "charlie"),
            _match_row("D", 4, "AA4", "delta"),
        ]
        compared = {
            tuple(sorted((left.row_id, right.row_id)))
            for left, right in candidate_pairs(rows, ["style_code"], 3)
        }
        self.assertEqual(compared, {("A", "B"), ("A", "C"), ("B", "C"), ("B", "D"), ("C", "D")})

    def test_rows_that_repeat_a_key_are_not_linked_to_themselves(self) -> None:
        rows = [
            _match_row("K1", 1, "OC100", "Navy Wool Coat"),
            _match_row("K1", 2, "DN200", "Raw Jean", "indigo"),
            _match_row("K2", 3, "OC100", "Wool Coat Navy"),
        ]
        pairs = candidate_pairs(rows, ["style_code", "title_prefix"], 3)
        self.assertTrue(all(left.row_id != right.row_id for left, right in pairs))
        links, _review, clusters = match_rows(rows, self.profile)
        self.assertEqual(
            [(link.left, link.left_source, link.right, link.right_source) for link in links],
            [("K1", 1, "K2", 3)],
        )
        self.assertEqual(clusters, [["K1", "K2"]])

    def test_token_swap_links_and_a_one_character_style_code_does_not(self) -> None:
        left, right = "navy wool coat", "coat wool navy"
        whole = cosine(qgrams(left, 3), qgrams(right, 3))
        tokens = cosine(token_qgrams(left, 3), token_qgrams(right, 3))
        self.assertEqual(whole, Decimal("0.5"))
        self.assertGreater(tokens, Decimal("0.999"))
        self.assertGreater(tokens, whole)
        self.assertEqual(monge_elkan(left, right), Decimal(1))
        self.assertLess(edit_similarity(left, right), Decimal("0.5"))
        self.assertEqual(jaro_winkler("kt300", "kt300"), Decimal(1))
        swapped = decide_pair(
            _match_row("L", 1, "OC100", "Navy Wool Coat", "navy"),
            _match_row("R", 2, "OC100", "Wool Coat Navy", "navy"),
            self.profile,
        )
        self.assertEqual(swapped.decision, "link")
        near = decide_pair(
            _match_row("L", 1, "KT300", "Grey Loop Tee", "grey"),
            _match_row("R", 2, "KT310", "Grey Loop Tee", "grey"),
            self.profile,
        )
        self.assertEqual(near.decision, "non-link")
        typo_profile = deepcopy(self.profile)
        typo_profile["dedup"]["edit_agree"] = 1
        linked = decide_pair(
            _match_row("L", 1, "KT300", "Grey Loop Tee", "grey"),
            _match_row("R", 2, "KT310", "Grey Loop Tee", "grey"),
            typo_profile,
        )
        self.assertEqual(linked.decision, "link")
        band = deepcopy(self.profile)
        band["dedup"]["possible_threshold"] = "-1"
        band["dedup"]["link_threshold"] = "2"
        possible = decide_pair(
            _match_row("L", 1, "KT300", "Grey Loop Tee", "grey"),
            _match_row("R", 2, "KT310", "Grey Loop Tee", "grey"),
            band,
        )
        self.assertEqual(possible.decision, "possible")

    def test_street_abbreviations_share_one_standard_form(self) -> None:
        self.assertEqual(standardize_text("44 W. 4th St."), "44 west fourth street")
        self.assertEqual(standardize_text("44 West Fourth Street"), "44 west fourth street")


class JcsTests(unittest.TestCase):
    def test_utf16_property_order_differs_from_code_point_order(self) -> None:
        keys = ["\r", "1", "\u0080", "\u00f6", "\u20ac", "\U0001f600", "\ufb33"]
        values = [
            "Carriage Return",
            "One",
            "Control",
            "Latin Small Letter O With Diaeresis",
            "Euro Sign",
            "Emoji: Grinning Face",
            "Hebrew Letter Dalet With Dagesh",
        ]
        payload = dict(zip(keys, values))
        utf16_order = sorted(keys, key=utf16_key)
        self.assertNotEqual(utf16_order, sorted(keys))
        self.assertLess(utf16_order.index("\U0001f600"), utf16_order.index("\ufb33"))
        text = canonicalize(payload)
        positions = [text.index(canonicalize(key)) for key in utf16_order]
        self.assertEqual(positions, sorted(positions))
        dumped = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        self.assertNotEqual(dumped, text)
        self.assertFalse(text.startswith(" ") or text.endswith(" "))

    def test_escapes_and_rejections(self) -> None:
        self.assertEqual(canonicalize("\u0001"), '"\\u0001"')
        self.assertEqual(canonicalize("\b\t\n\f\r"), '"\\b\\t\\n\\f\\r"')
        self.assertEqual(canonicalize('say "hi" \\'), '"say \\"hi\\" \\\\"')
        self.assertEqual(canonicalize(True), "true")
        self.assertEqual(canonicalize(False), "false")
        self.assertEqual(canonicalize(2**53 - 1), str(2**53 - 1))
        self.assertEqual(canonicalize("26000.33"), '"26000.33"')
        with self.assertRaises(JCSError):
            canonicalize(26000.33)
        with self.assertRaises(JCSError):
            canonicalize(2.0)
        with self.assertRaises(JCSError):
            canonicalize(1.5)
        with self.assertRaises(JCSError):
            canonicalize(float("nan"))
        with self.assertRaises(JCSError):
            canonicalize(float("inf"))
        with self.assertRaises(JCSError):
            canonicalize(2**53)
        with self.assertRaises(JCSError):
            canonicalize("\ud800")
        with self.assertRaises(JCSError):
            canonicalize_items([("a", 1), ("a", 2)])
        with self.assertRaises(JCSError):
            assert_ascii_keys({"\u00f6": 1})
        assert_ascii_keys({"ok": [1, {"also": None}]})
        self.assertEqual(canonicalize_json(' { "b" : 1, "a" : 2 } '), '{"a":2,"b":1}')
        with self.assertRaises(JCSError):
            canonicalize_json('{"a": 1, "a": 2}')
        with self.assertRaises(JCSError):
            canonicalize_json('{"a": NaN}')


def _match_row(row_id: str, source: int, style: str, title: str, color: str = "navy") -> ViewRow:
    return ViewRow(
        row_id=row_id,
        source_row=source,
        output_row=source,
        values={"style_code": style, "title": title, "color": color},
        raw={},
        match={
            "style_code": standardize_text(style),
            "title": standardize_text(title),
            "color": standardize_text(color),
        },
    )


if __name__ == "__main__":
    unittest.main()
