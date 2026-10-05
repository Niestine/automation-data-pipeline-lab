"""Version tuple oracles. The checked-in table must score 100%."""

from __future__ import annotations

import json
import unittest

import helpers  # noqa: F401

from contract_lab.errors import VersionParseError
from contract_lab.versions import classify_change, parse_version
from helpers import EXAMPLES


class VersionTests(unittest.TestCase):
    def test_checked_in_oracle_is_exact(self) -> None:
        table = json.loads((EXAMPLES / "version_oracle.json").read_text(encoding="utf-8"))
        passed = 0
        total = 0
        for row in table["parse"]:
            total += 1
            parsed = parse_version(row["input"])
            self.assertEqual(parsed.as_tuple(), (row["major"], row["minor"], row["patch"], row["label"]))
            passed += 1
        for row in table["compare"]:
            total += 1
            kind = classify_change(parse_version(row["left"]), parse_version(row["right"]))
            self.assertEqual(kind, row["kind"])
            passed += 1
        for value in table["reject"]:
            total += 1
            with self.assertRaises(VersionParseError):
                parse_version(value)
            passed += 1
        self.assertEqual(passed, total)
        self.assertEqual(passed / total, 1.0)

    def test_integer_minor_step_is_an_upgrade(self) -> None:
        kind = classify_change(parse_version("1.9.0"), parse_version("1.10.0"))
        self.assertEqual(kind, "minor_upgrade")

    def test_label_graduation_is_not_a_patch(self) -> None:
        kind = classify_change(parse_version("1.2.3-alpha"), parse_version("1.2.3"))
        self.assertEqual(kind, "label_change")

    def test_prefix_v_is_case_insensitive(self) -> None:
        self.assertEqual(parse_version("V1").as_tuple(), (1, 0, 0, ""))
        self.assertEqual(parse_version("v1.2").as_tuple(), (1, 2, 0, ""))

    def test_rejections_outside_the_oracle_file(self) -> None:
        for value in ("1.2.3.4", "1000.0.0", "1.2.3-", "1.2.3-Alpha", "1.2.3-rc.1", "v", ""):
            with self.assertRaises(VersionParseError):
                parse_version(value)

    def test_downgrade_and_label_kinds(self) -> None:
        self.assertEqual(
            classify_change(parse_version("2.0.0"), parse_version("1.9.9")),
            "major_downgrade",
        )
        self.assertEqual(
            classify_change(parse_version("1.2.3"), parse_version("1.2.1")),
            "patch_downgrade",
        )
        self.assertEqual(
            classify_change(parse_version("1.2.3-rc"), parse_version("1.2.3-rc")),
            "no_change",
        )
