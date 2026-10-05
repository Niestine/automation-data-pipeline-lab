import helpers  # noqa: F401
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from incremental_crawl_lab.__main__ import execute
from incremental_crawl_lab.store import Store


class CliTest(unittest.TestCase):
    def test_dry_run_report_and_bad_arguments(self) -> None:
        code, report = execute(
            [
                "--config",
                str(helpers.EXAMPLES / "config.json"),
                "--site",
                str(helpers.EXAMPLES / "site.json"),
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(report["dry_run"])
        self.assertIsNone(report["state_dir"])
        self.assertEqual(report["not_modified_304"], 2)
        self.assertEqual(report["material_change_count"], 1)
        self.assertEqual(report["requests"], 10)
        self.assertEqual(report["robots_blocks"], 2)
        self.assertEqual(report["freshness_source"], "fixture_oracle")
        missing = execute(["--config", "missing.json", "--site", "missing.json", "--dry-run"])
        self.assertEqual(missing[0], 2)
        self.assertEqual(execute(["--weeks", "0", "--config", "x", "--site", "y", "--dry-run"])[0], 2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(execute(["--config"])[0], 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(execute(["--help"]), (0, {}))

    def test_state_dir_and_crash_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            code, report = execute(
                [
                    "--config",
                    str(helpers.EXAMPLES / "config.json"),
                    "--site",
                    str(helpers.EXAMPLES / "site.json"),
                    "--state-dir",
                    str(state),
                    "--weeks",
                    "1",
                    "--log-jsonl",
                    str(Path(tmp) / "events.jsonl"),
                ]
            )
            self.assertEqual(code, 0)
            self.assertFalse(report["dry_run"])
            self.assertTrue((state / "crawl.sqlite").is_file())
            self.assertTrue((state / "report.json").is_file())
            self.assertTrue((state / "collection.jsonl").is_file())
            saved = json.loads((state / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["retries"], 0)
            self.assertNotIn("password", json.dumps(saved))

            crash_dir = Path(tmp) / "crash"
            code, payload = execute(
                [
                    "--config",
                    str(helpers.EXAMPLES / "config.json"),
                    "--site",
                    str(helpers.EXAMPLES / "site.json"),
                    "--state-dir",
                    str(crash_dir),
                    "--weeks",
                    "1",
                    "--crash-at",
                    "pre_request",
                ]
            )
            self.assertEqual(code, 3)
            self.assertEqual(payload["crash"], "pre_request")
            store = Store.open(str(crash_dir / "crawl.sqlite"), durable=True)
            inflight = [row for row in store.list_status("in_flight")]
            self.assertEqual(len(inflight), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
