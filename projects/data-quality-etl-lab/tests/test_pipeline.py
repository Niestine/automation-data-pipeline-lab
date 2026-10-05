"""Seal the synthetic week drop and the operational edges around it."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest import mock

import support
from harbor_ledger import load_profile, load_schema, run_bytes, run_path
from harbor_ledger.jcs import canonicalize_json

ROOT = support.ROOT
EXAMPLES = support.EXAMPLES
SCHEMA = load_schema(EXAMPLES / "schema.json")
PROFILE = load_profile(EXAMPLES / "profile.json")
DROP = (EXAMPLES / "supplier_drop.csv").read_bytes()

CLEAN_IDS = [
    "HL001",
    "HL003",
    "HL005",
    "HL006",
    "HL007",
    "HL008",
    "HL012",
    "HL013",
    "HL014",
    "HL015",
    "HL016",
]
ABSENT_IDS = ["HL002", "HL004", "HL009", "HL010", "HL011"]
FINDING_KEYS = {
    ("HL001", "title", "duplicate", "duplicated_value", "link"),
    ("HL002", "title", "duplicate", "duplicated_value", "link"),
    ("HL003", "department", "constraint", "constraint_break", "style_department"),
    ("HL003", "style_code", "constraint", "constraint_break", "style_department"),
    ("HL003", "title", "duplicate", "duplicated_value", "link"),
    ("HL004", "department", "constraint", "constraint_break", "style_department"),
    ("HL004", "style_code", "constraint", "constraint_break", "style_department"),
    ("HL004", "title", "duplicate", "duplicated_value", "link"),
    ("HL006", "department", "constraint", "constraint_break", "east_knit"),
    ("HL006", "supplier_code", "constraint", "constraint_break", "east_knit"),
    ("HL007", "wholesale_usd", "constraint", "constraint_break", "range:wholesale_usd"),
    ("HL007", "wholesale_usd", "outlier", "outlier", "modified_z"),
    ("HL008", "style_code", "pattern_type", "bogus", "pattern:style_code"),
    ("HL009", "wholesale_usd", "constraint", "missing", "required:wholesale_usd"),
    ("HL010", "listed_on", "pattern_type", "bogus", "type:listed_on"),
    ("HL011", "color", "pattern_type", "bogus", "type:color"),
    ("HL011", "department", "pattern_type", "bogus", "type:department"),
    ("HL011", "listed_on", "pattern_type", "bogus", "type:listed_on"),
    ("HL011", "listing_id", "pattern_type", "bogus", "ragged"),
    ("HL011", "title", "pattern_type", "bogus", "type:title"),
    ("HL011", "wholesale_usd", "pattern_type", "bogus", "type:wholesale_usd"),
}


def _signature(finding: dict) -> tuple:
    return (
        finding["record_key"],
        finding["column"],
        finding["detector"],
        finding["class"],
        finding["rule"],
    )


class DemoSealTests(unittest.TestCase):
    def test_week_drop_seals_the_same_catalog_twice(self) -> None:
        with self.assertLogs("harbor_ledger", level="INFO") as captured:
            first = run_bytes(DROP, SCHEMA, PROFILE)
            second = run_bytes(DROP, SCHEMA, PROFILE)
        self.assertEqual(first.status, "ok")
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.clean_csv, second.clean_csv)
        self.assertTrue(any("detector union sealed" in line for line in captured.output))
        self.assertEqual(first.report["clusters"], [["HL001", "HL002"], ["HL003", "HL004"]])
        self.assertEqual([row["listing_id"] for row in first.report["clean_rows"]], CLEAN_IDS)
        for listing_id in ABSENT_IDS:
            self.assertNotIn(listing_id, first.clean_csv.decode("utf-8"))
        self.assertEqual({_signature(item) for item in first.findings}, FINDING_KEYS)
        folds = [item for item in first.report["annotations"] if item["kind"] == "nfkc"]
        self.assertEqual(len(folds), 1)
        self.assertEqual(folds[0]["source_row"], 11)
        self.assertEqual(folds[0]["column"], "style_code")
        self.assertEqual(folds[0]["before"], "ＫＴ３１０")
        self.assertEqual(folds[0]["after"], "KT310")
        published = {row["listing_id"]: row for row in first.report["clean_rows"]}
        self.assertEqual(published["HL008"]["style_code"], "ＫＴ３１０")
        self.assertIn("ＫＴ３１０".encode("utf-8"), first.clean_csv)
        self.assertEqual(published["HL012"]["title"], "Coat, Hooded")
        self.assertIn(b'"Coat, Hooded"', first.clean_csv)
        self.assertEqual(published["HL001"]["wholesale_usd"], "86.00")
        self.assertEqual(published["HL007"]["wholesale_usd"], "910.00")
        self.assertIsInstance(published["HL007"]["wholesale_usd"], str)
        self.assertEqual(first.report["review"], [])
        self.assertEqual(len(first.report["links"]), 2)
        self.assertTrue(all(link["decision"] == "link" for link in first.report["links"]))
        self.assertTrue(all(link["weight"] == "13.047209" for link in first.report["links"]))
        self.assertTrue(all(link["skipped"] == [] for link in first.report["links"]))
        self.assertEqual(first.report["unicode_version"], unicodedata.unidata_version)
        self.assertEqual(canonicalize_json(json.dumps(first.report)).encode("utf-8"), first.report_jcs)
        self.assertEqual(first.manifest["report_sha256"], hashlib.sha256(first.report_jcs).hexdigest())
        self.assertEqual(first.manifest["input_sha256"], hashlib.sha256(DROP).hexdigest())
        self.assertEqual(first.manifest["schema_name"], "harbor-ledger-week-drop")
        self.assertEqual(first.manifest["seed"], 4180)
        self.assertEqual(first.manifest["window"], 3)
        self.assertFalse(first.manifest["repair_between_detectors"])
        self.assertIsNone(first.manifest["injector_shortfall"])
        self.assertEqual(first.manifest["encoding"], "utf-8")
        self.assertFalse(first.manifest["bom_override"])
        self.assertEqual(
            first.manifest["normalization"]["identifier_fold"],
            ["listing_id", "style_code"],
        )
        self.assertFalse(first.clean_csv.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(first.clean_csv.endswith(b"\r\n"))

    def test_clean_csv_is_a_fixed_point_and_its_digest_is_not(self) -> None:
        first = run_bytes(DROP, SCHEMA, PROFILE)
        second = run_bytes(first.clean_csv, SCHEMA, PROFILE, owned=True)
        self.assertEqual(second.status, "ok")
        self.assertEqual(first.clean_csv, second.clean_csv)
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(
            {_signature(item) for item in second.findings},
            {
                ("HL006", "department", "constraint", "constraint_break", "east_knit"),
                ("HL006", "supplier_code", "constraint", "constraint_break", "east_knit"),
                ("HL007", "wholesale_usd", "constraint", "constraint_break", "range:wholesale_usd"),
                ("HL007", "wholesale_usd", "outlier", "outlier", "modified_z"),
                ("HL008", "style_code", "pattern_type", "bogus", "pattern:style_code"),
            },
        )

    def test_clean_unique_rows_keep_their_digest_when_reordered(self) -> None:
        header = "listing_id,supplier_code,style_code,department,title,color,wholesale_usd,listed_on"
        rows = [
            "HL001,NORTH,OC100,outerwear,Navy Wool Coat,navy,86.00,2026-03-02",
            "HL005,EAST,KT300,knit,Grey Loop Tee,grey,18.00,2026-02-11",
            "HL013,NORTH,OC120,outerwear,Field Parka,olive,64.00,2026-03-06",
        ]
        forward = ("# note\n\n" + header + "\n" + "\n".join(rows) + "\n").encode("utf-8")
        backward = ("# note\n\n" + header + "\n" + "\n".join(reversed(rows)) + "\n").encode("utf-8")
        first = run_bytes(forward, SCHEMA, PROFILE)
        second = run_bytes(backward, SCHEMA, PROFILE)
        self.assertEqual(first.findings, [])
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.clean_csv, second.clean_csv)

    def test_moving_a_dirty_line_changes_the_digest_only(self) -> None:
        lines = DROP.decode("utf-8").splitlines()
        left = next(index for index, line in enumerate(lines) if line.startswith("HL006,"))
        right = next(index for index, line in enumerate(lines) if line.startswith("HL007,"))
        swapped = list(lines)
        swapped[left], swapped[right] = swapped[right], swapped[left]
        original = ("\n".join(lines) + "\n").encode("utf-8")
        moved = ("\n".join(swapped) + "\n").encode("utf-8")
        first = run_bytes(original, SCHEMA, PROFILE)
        second = run_bytes(moved, SCHEMA, PROFILE)
        self.assertEqual(first.clean_csv, second.clean_csv)
        self.assertNotEqual(first.digest, second.digest)

    def test_frozen_profile_does_not_refit_or_retune(self) -> None:
        def refuse(*_args, **_kwargs):
            raise AssertionError("frozen profile was refit")

        with mock.patch("harbor_ledger.dedup.estimate_m", refuse), mock.patch(
            "harbor_ledger.dedup.estimate_u", refuse
        ), mock.patch("harbor_ledger.metrics.suggest_outlier_threshold", refuse):
            result = run_bytes(DROP, SCHEMA, PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertFalse(PROFILE["dedup"]["refit"])

    def test_oracle_scores_count_findings_on_rows_the_oracle_calls_clean(self) -> None:
        oracle = [
            {"row_id": "HL007", "column": "wholesale_usd", "class": "outlier"},
            {"row_id": "HL006", "column": "department", "class": "constraint_break"},
            {"row_id": "HL013", "column": "title", "class": "typo", "slice": "train"},
        ]
        result = run_bytes(DROP, SCHEMA, PROFILE, oracle=oracle)
        evaluation = result.report["metrics"]["evaluation"]
        outlier = evaluation["outlier"]["outlier"]
        self.assertEqual((outlier["tp"], outlier["fp"], outlier["fn"]), (1, 0, 0))
        constraint = evaluation["constraint"]["constraint_break"]
        # HL003, HL004, HL006 supplier_code, and HL007 are outside the oracle and count against precision.
        self.assertEqual((constraint["tp"], constraint["fp"], constraint["fn"]), (1, 6, 0))
        self.assertEqual(constraint["precision"], "0.142857")
        training = result.report["metrics"]["training"]["pattern_type"]["typo"]
        self.assertEqual((training["tp"], training["fn"], training["upper_recall"]), (0, 1, "1.000000"))
        self.assertNotEqual(result.digest, run_bytes(DROP, SCHEMA, PROFILE).digest)

    def test_repeated_key_keeps_the_first_sound_row_and_never_self_links(self) -> None:
        header = "listing_id,supplier_code,style_code,department,title,color,wholesale_usd,listed_on\n"
        text = (
            header
            + "HL001,NORTH,OC100,outerwear,Navy Wool Coat,navy,86.00,2026-03-02\n"
            + "HL001,SOUTH,DN200,denim,Raw Selvedge Jean,indigo,72.50,2026-03-04\n"
            + "HL002,NORTH,OC120,outerwear,Field Parka,olive,bad,2026-03-06\n"
            + "HL002,NORTH,OC120,outerwear,Field Parka,olive,64.00,2026-03-06\n"
        )
        result = run_bytes(text.encode("utf-8"), SCHEMA, PROFILE)
        self.assertEqual(result.report["links"], [])
        self.assertEqual(result.report["clusters"], [])
        published = [(row["listing_id"], row["style_code"], row["wholesale_usd"]) for row in result.report["clean_rows"]]
        self.assertEqual(published, [("HL001", "OC100", "86.00"), ("HL002", "OC120", "64.00")])
        keys = [item for item in result.findings if item["rule"] == "primary_key"]
        self.assertEqual(sorted(item["source_row"] for item in keys), [2, 3, 4, 5])

    def test_a_ragged_row_does_not_join_a_functional_dependency_conflict(self) -> None:
        header = "listing_id,supplier_code,style_code,department,title,color,wholesale_usd,listed_on\n"
        text = (
            header
            + "HL001,NORTH,OC100,outerwear,Coat A,navy,86.00,2026-03-02\n"
            + "HL002,NORTH,OC100,knit,Coat B,red,86.00,2026-03-02\n"
            + "HL003,NORTH,OC100\n"
        )
        result = run_bytes(text.encode("utf-8"), SCHEMA, PROFILE)
        fd_rows = {item["record_key"] for item in result.findings if item["rule"] == "style_department"}
        self.assertEqual(fd_rows, {"HL001", "HL002"})


class OperationalTests(unittest.TestCase):
    def test_dry_run_retry_quarantine_and_fatal_decode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slept: list[float] = []
            attempts = {"count": 0}

            def reader(_path: str) -> bytes:
                attempts["count"] += 1
                if attempts["count"] < 3:
                    raise OSError("busy")
                return DROP

            dry = root / "dry"
            result = run_path(
                EXAMPLES / "supplier_drop.csv",
                SCHEMA,
                PROFILE,
                out_dir=dry,
                dry_run=True,
                reader=reader,
                sleeper=slept.append,
            )
            self.assertEqual(result.status, "ok")
            self.assertEqual(slept, [0.0, 0.001])
            self.assertFalse(dry.exists())
            self.assertIsNotNone(result.clean_csv)

            quarantine_dir = root / "quarantine"
            bad = root / "bad.csv"
            bad.write_bytes(b"\xff\xff")
            quarantined = run_path(bad, SCHEMA, PROFILE, out_dir=quarantine_dir)
            self.assertEqual(quarantined.status, "quarantined")
            self.assertFalse((quarantine_dir / "clean.csv").exists())
            self.assertTrue((quarantine_dir / "report.jcs").is_file())
            self.assertTrue((quarantine_dir / "run_manifest.json").is_file())

            fatal_dir = root / "fatal"
            owned = root / "owned.csv"
            owned.write_bytes(b"\xff")
            fatal = run_path(owned, SCHEMA, PROFILE, out_dir=fatal_dir, owned=True)
            self.assertEqual(fatal.status, "fatal")
            self.assertFalse(fatal_dir.exists())
            self.assertIsNone(fatal.clean_csv)

            missing_header = run_bytes(b"# comment only\n", SCHEMA, PROFILE)
            self.assertEqual(missing_header.status, "fatal")
            self.assertIsNone(missing_header.report)
            ragged = run_bytes(b"a,b\n1,2\n", SCHEMA, PROFILE)
            self.assertEqual(ragged.status, "fatal")

    def test_exhausted_retries_raise_the_last_os_error(self) -> None:
        slept: list[float] = []

        def reader(_path: str) -> bytes:
            raise OSError("still busy")

        with self.assertRaises(OSError):
            run_path(
                EXAMPLES / "supplier_drop.csv",
                SCHEMA,
                PROFILE,
                reader=reader,
                sleeper=slept.append,
            )
        self.assertEqual(slept, [0.0, 0.001])

    def test_two_processes_agree_on_the_digest_and_clean_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_dir = root / "one"
            second_dir = root / "two"
            first = _run_cli(first_dir)
            second = _run_cli(second_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertIn("status=ok digest=", first.stdout)
            self.assertEqual((first_dir / "clean.csv").read_bytes(), (second_dir / "clean.csv").read_bytes())
            self.assertEqual((first_dir / "report.jcs").read_bytes(), (second_dir / "report.jcs").read_bytes())

    def test_cli_fatal_owned_file_returns_two_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owned = root / "owned.csv"
            owned.write_bytes(b"\xff")
            dest = root / "out"
            completed = _run_cli(dest, extra=["--owned", "--input", str(owned)])
            self.assertEqual(completed.returncode, 2)
            self.assertFalse(dest.exists())

    def test_cli_exit_codes_for_a_bad_profile_a_quarantine_and_an_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = json.loads((EXAMPLES / "profile.json").read_text(encoding="utf-8"))
            broken["detector_order"] = ["pattern_type", "spellcheck"]
            bad_profile = root / "bad_profile.json"
            bad_profile.write_text(json.dumps(broken), encoding="utf-8")
            rejected = _run_cli(root / "rejected", extra=["--profile", str(bad_profile)])
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("detector_order", rejected.stderr)
            self.assertFalse((root / "rejected").exists())

            mangled = root / "mangled.csv"
            mangled.write_bytes(b"listing_id\xff\n")
            quarantined = _run_cli(root / "quarantine", extra=["--input", str(mangled)])
            self.assertEqual(quarantined.returncode, 0, quarantined.stderr)
            self.assertIn("status=quarantined", quarantined.stdout)
            self.assertFalse((root / "quarantine" / "clean.csv").exists())
            self.assertTrue((root / "quarantine" / "report.jcs").is_file())

            oracle = root / "oracle.json"
            oracle.write_text(
                json.dumps({"cells": [{"row_id": "HL007", "column": "wholesale_usd", "class": "outlier"}]}),
                encoding="utf-8",
            )
            scored = _run_cli(root / "scored", extra=["--oracle", str(oracle)])
            self.assertEqual(scored.returncode, 0, scored.stderr)
            report = json.loads((root / "scored" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["metrics"]["evaluation"]["outlier"]["outlier"]["recall"], "1.000000")


def _run_cli(out_dir: Path, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(ROOT / "run_lab.py"),
        "--input",
        str(EXAMPLES / "supplier_drop.csv"),
        "--schema",
        str(EXAMPLES / "schema.json"),
        "--profile",
        str(EXAMPLES / "profile.json"),
        "--out-dir",
        str(out_dir),
    ]
    if extra:
        command.extend(extra)
    return subprocess.run(command, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
