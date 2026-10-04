import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401
from helpers import EXAMPLES

from maintenance_lab.__main__ import default_examples_dir, main, run_lab
from maintenance_lab.bugs import BUGS
from maintenance_lab.seed import (
    EU_CSV,
    JP_CSV,
    LATIN1_CSV,
    NEWLINE_CSV,
    V1_CSV,
    V2_JSON,
    catalog_payload,
    jp_cp932_bytes,
    latin1_bytes,
)


class CliTests(unittest.TestCase):
    def test_run_lab_offline_default(self):
        payload = run_lab([])
        self.assertEqual(payload["week_id"], "2026-W01")
        self.assertFalse(payload["dry_run"])
        self.assertEqual(payload["counts"]["inserted"], 8)
        self.assertEqual(payload["counts"]["updated"], 3)
        self.assertEqual(payload["counts"]["replayed"], 2)
        self.assertEqual(payload["counts"]["conflict"], 1)
        self.assertEqual(payload["counts"]["rejected"], 3)
        self.assertEqual(payload["catalog_size"], 12)
        self.assertIn("SKU-1001", {row["sku"] for row in payload["products"]})
        self.assertTrue(payload["state_dir"])

    def test_dry_run_preview_does_not_grow_catalog_size(self):
        payload = run_lab(["--dry-run"])
        self.assertTrue(payload["dry_run"])
        self.assertIsNone(payload["state_dir"])
        self.assertEqual(payload["catalog_size"], 4)
        self.assertGreater(payload["counts"]["inserted"], 0)
        self.assertEqual(payload["compat"]["added"], [])

    def test_list_bugs(self):
        payload = run_lab(["--list-bugs"])
        self.assertEqual(len(payload["bugs"]), len(BUGS))
        self.assertEqual(payload["bugs"][0]["id"], "BUG-001")

    def test_compat_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = Path(tmp) / "a.json"
            after = Path(tmp) / "b.json"
            before.write_text(json.dumps(catalog_payload()), encoding="utf-8")
            data = catalog_payload()
            data["products"] = [row for row in data["products"] if row["sku"] != "SKU-1002"]
            after.write_text(json.dumps(data), encoding="utf-8")
            payload = run_lab(["--compat-from", str(before), "--compat-to", str(after)])
            self.assertEqual(payload["compat"]["removed"], ["SKU-1002"])

    def test_main_prints_json(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--dry-run"])
        self.assertEqual(code, 0)
        parsed = json.loads(buffer.getvalue())
        self.assertIn("counts", parsed)

    def test_missing_feed_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope.csv")
            stderr = io.StringIO()
            with patch("sys.stderr", new=stderr), redirect_stdout(io.StringIO()):
                code = main(["--feed", missing])
        self.assertEqual(code, 2)
        self.assertIn("could not load", stderr.getvalue())

    def test_malformed_catalog_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "catalog.json"
            bad.write_text("{not json", encoding="utf-8")
            stderr = io.StringIO()
            with patch("sys.stderr", new=stderr), redirect_stdout(io.StringIO()):
                code = main(["--catalog", str(bad), "--dry-run"])
        self.assertEqual(code, 2)

    def test_compat_requires_both_flags(self):
        stderr = io.StringIO()
        with patch("sys.stderr", new=stderr), redirect_stdout(io.StringIO()):
            code = main(["--compat-from", str(EXAMPLES / "catalog.json")])
        self.assertEqual(code, 2)

    def test_crash_after_rows_exits_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with redirect_stdout(stdout), patch("sys.stderr", new=io.StringIO()):
                code = main(["--state-dir", tmp, "--crash-after-rows", "1"])
        self.assertEqual(code, 3)
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["error"], "simulated_crash")

    def test_default_examples_dir_resolves_inside_project(self):
        self.assertEqual(default_examples_dir().resolve(), EXAMPLES.resolve())

    def test_unknown_flag_exits(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            run_lab(["--not-a-flag"])

    def test_print_events_included(self):
        payload = run_lab(["--dry-run", "--print-events"])
        events = [item["event"] for item in payload["events"]]
        self.assertIn("run_start", events)
        self.assertIn("decode_ok", events)

    def test_list_bugs_main_exit_0(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--list-bugs"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(buffer.getvalue())["bugs"]), 16)


    def test_main_survives_non_utf8_console(self):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp932", errors="strict", newline="")
        with patch("sys.stdout", new=console), patch("sys.stderr", new=io.StringIO()):
            code = main([])
            console.flush()
        self.assertEqual(code, 0)
        parsed = json.loads(raw.getvalue().decode("cp932"))
        titles = {row["sku"]: row["title"] for row in parsed["products"]}
        self.assertEqual(titles["EU-4001"], "Café Linen")
        self.assertEqual(titles["JP-2001"], "木綿シャツ")

    def test_crash_then_resume_with_same_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()), patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(main(["--state-dir", tmp, "--crash-after-rows", "1"]), 3)
            self.assertTrue((Path(tmp) / "checkpoints" / "2026-W01__supplier_v1_csv.json").exists())
            resumed = run_lab(["--state-dir", tmp])
            self.assertEqual(resumed["catalog_size"], 12)
            self.assertEqual(resumed["counts"]["inserted"], 8)
            v1 = resumed["feeds"][0]
            self.assertNotIn(1, [item["index"] for item in v1["outcomes"]])
            products = {row["sku"]: row for row in resumed["products"]}
            self.assertEqual(products["SKU-1001"]["version"], 2)
            self.assertEqual(products["SKU-1001"]["price_cents"], 2150)
            again = run_lab(["--state-dir", tmp])
            self.assertEqual(again["counts"]["feed_replays"], 6)
            self.assertEqual(again["counts"]["rows"], 0)
            forced = run_lab(["--state-dir", tmp, "--force"])
            self.assertEqual(forced["counts"]["rows"], 17)
            self.assertEqual(forced["counts"]["inserted"], 0)
            self.assertEqual(forced["catalog_size"], 12)

    def test_dry_run_reads_state_dir_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_lab(["--state-dir", tmp])
            before = {path: path.read_bytes() for path in Path(tmp).rglob("*") if path.is_file()}
            preview = run_lab(["--state-dir", tmp, "--dry-run"])
            after = {path: path.read_bytes() for path in Path(tmp).rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(preview["counts"]["feed_replays"], 6)
            self.assertEqual(preview["catalog_size"], 12)
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "fresh"
            run_lab(["--state-dir", str(empty), "--dry-run"])
            self.assertFalse(empty.exists())

    def test_example_files_match_seed(self):
        feeds = EXAMPLES / "feeds"
        expected = {
            "supplier_v1.csv": V1_CSV.encode("utf-8"),
            "supplier_v1_jp.cp932": jp_cp932_bytes(),
            "supplier_v1_jp.csv": JP_CSV.encode("utf-8"),
            "supplier_eu.csv": EU_CSV.encode("utf-8"),
            "supplier_v2.json": V2_JSON.encode("utf-8"),
            "supplier_latin1.latin1": latin1_bytes(),
            "supplier_latin1.csv": LATIN1_CSV.encode("utf-8"),
            "supplier_newline.csv": NEWLINE_CSV.encode("utf-8"),
        }
        self.assertEqual(sorted(path.name for path in feeds.iterdir()), sorted(expected))
        for name, data in expected.items():
            self.assertEqual((feeds / name).read_bytes(), data, name)
        on_disk = json.loads((EXAMPLES / "catalog.json").read_text(encoding="utf-8"))
        seeded = catalog_payload()
        for row in seeded["products"]:
            row.pop("fingerprint")
        self.assertEqual(on_disk, seeded)

    def test_example_feed_files_reproduce_default_run(self):
        names = [
            "supplier_v1.csv",
            "supplier_v1_jp.cp932",
            "supplier_eu.csv",
            "supplier_v2.json",
            "supplier_latin1.latin1",
            "supplier_newline.csv",
        ]
        argv = ["--dry-run"]
        for name in names:
            argv += ["--feed", str(EXAMPLES / "feeds" / name)]
        self.assertEqual(run_lab(argv)["counts"], run_lab(["--dry-run"])["counts"])

    def test_readable_jp_copy_fails_loudly_as_a_feed(self):
        stderr = io.StringIO()
        with patch("sys.stderr", new=stderr), redirect_stdout(io.StringIO()):
            code = main(["--dry-run", "--feed", str(EXAMPLES / "feeds" / "supplier_v1_jp.csv")])
        self.assertEqual(code, 1)
        self.assertIn("decode_error: declared encoding cp932 failed", stderr.getvalue())

    def test_log_jsonl_streams_structured_events(self):
        stderr = io.StringIO()
        with patch("sys.stderr", new=stderr):
            run_lab(["--dry-run", "--log-jsonl"])
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual(events[0]["event"], "run_start")
        self.assertEqual(events[-1]["event"], "run_complete")
        self.assertTrue(all(item["ts_ms"] == 1767528000000 for item in events))
        rejected = [item for item in events if item["event"] == "row_rejected"]
        self.assertEqual(len(rejected), 3)
        slash = next(item for item in rejected if item["sku"] == "SKU-1006")
        self.assertEqual(slash["bug_guards"], ["BUG-015"])
        self.assertEqual(slash["field"], "updated_at")
        self.assertEqual(slash["line"], 10)
        decoded = {item["name"]: item["encoding"] for item in events if item["event"] == "decode_ok"}
        self.assertEqual(decoded["supplier_v1_jp.cp932"], "cp932")
        self.assertEqual(decoded["supplier_latin1.csv"], "latin-1")

if __name__ == "__main__":
    unittest.main()
