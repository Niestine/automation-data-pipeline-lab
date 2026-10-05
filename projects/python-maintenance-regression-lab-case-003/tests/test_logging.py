"""DEBUG schedule lines stay in the run file. INFO summaries reach the console."""

from __future__ import annotations

import logging
import tempfile
import helpers  # noqa: F401
import unittest
from pathlib import Path

from schedlab.campaign import run_campaign


class LoggingSplitTests(unittest.TestCase):
    def test_file_keeps_debug_and_console_does_not(self) -> None:
        root_level = logging.getLogger().level
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            console = helpers.silence()
            run_campaign(
                "ordering_d1",
                "buggy",
                "pct",
                seeds=[0],
                d=1,
                n_max=2,
                artifact_dir=directory,
                console_stream=console,
            )
            logs = list(directory.glob("*.log"))
            artifacts = list(directory.glob("*.json"))
            self.assertEqual(len(logs), 1)
            self.assertEqual(len(artifacts), 1)
            log_text = logs[0].read_text(encoding="utf-8")
            console_text = console.getvalue()
            self.assertIn("tid=", log_text)
            self.assertIn("DEBUG", log_text)
            self.assertIn("schedlab.schedule", log_text)
            self.assertRegex(log_text, r"\d{4}-\d{2}-\d{2}")
            self.assertIn("INFO", console_text)
            self.assertIn("schedlab.campaign", console_text)
            self.assertIn("terminal=", console_text)
            self.assertNotIn("tid=", console_text)
            self.assertNotIn("/tmp/myapp.log", logs[0].as_posix())
            self.assertNotIn("/tmp/myapp.log", artifacts[0].as_posix())
            self.assertTrue(logs[0].is_relative_to(directory))
            self.assertTrue(artifacts[0].is_relative_to(directory))
            first_bytes = artifacts[0].read_bytes()
            run_campaign(
                "ordering_d1",
                "buggy",
                "pct",
                seeds=[1],
                d=1,
                n_max=2,
                artifact_dir=directory,
                console_stream=helpers.silence(),
            )
            self.assertEqual(artifacts[0].read_bytes(), first_bytes)
            second = [path for path in directory.glob("*.json") if path != artifacts[0]]
            self.assertGreaterEqual(len(second), 1)
        self.assertEqual(logging.getLogger().level, root_level)
        leaked = [handler for handler in logging.getLogger().handlers if getattr(handler, "_schedlab", False)]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
