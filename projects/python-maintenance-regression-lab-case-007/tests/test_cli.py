"""CLI JSON for one notice, the timer table, and the frozen trace."""

from __future__ import annotations

import json
import unittest

import helpers
from bay_notice.__main__ import main


class CliTests(unittest.TestCase):
    def test_run_prints_the_repaired_notice(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with redirect_stdout(StringIO()):
            code = main(["run", str(helpers.EXAMPLES / "gate_schedule.json")])
        self.assertEqual(code, 0)

    def test_run_json_shape(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buffer = StringIO()
        with redirect_stdout(buffer):
            code = main(["run", str(helpers.EXAMPLES / "gate_schedule.json"), "--build", "repaired"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["operation_id"], "BAY-1001")
        self.assertEqual(payload["transmissions"], 2)
        self.assertEqual(payload["commits"], 1)
        self.assertEqual(payload["body"], "HOLD C-14 40min")
        self.assertEqual(payload["delays_s"], [0.25])
        self.assertEqual(payload["decisions"], ["retry", "stop"])
        self.assertTrue(payload["ok"])

    def test_bad_charge_exits_2(self) -> None:
        from io import StringIO
        from contextlib import redirect_stderr

        buffer = StringIO()
        with redirect_stderr(buffer):
            code = main(["run", str(helpers.EXAMPLES / "bad_charge.json")])
        self.assertEqual(code, 2)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["error"], "JobRejected")

    def test_malformed_fixtures_exit_2_without_a_traceback(self) -> None:
        import tempfile
        from contextlib import redirect_stderr
        from io import StringIO
        from pathlib import Path

        job = (
            '"job": {"operation_id": "BAY-1001", "bay": "C-14", '
            '"body": "HOLD C-14 40min", "charge": "18.00"}'
        )
        cases = {
            "unknown_kind.json": "{" + job + ', "steps": [{"kind": "melt"}]}',
            "unknown_error.json": "{" + job + ', "steps": [{"kind": "error", "error_name": "melt"}]}',
            "no_job.json": '{"steps": []}',
            "not_json.json": "{",
        }
        with tempfile.TemporaryDirectory() as folder:
            for name, text in cases.items():
                path = Path(folder) / name
                path.write_text(text, encoding="utf-8")
                buffer = StringIO()
                with redirect_stderr(buffer):
                    code = main(["run", str(path)])
                self.assertEqual(code, 2, name)
                self.assertIn("error", json.loads(buffer.getvalue()), name)
            buffer = StringIO()
            with redirect_stderr(buffer):
                code = main(["run", str(Path(folder) / "missing.json")])
            self.assertEqual(code, 2)

    def test_log_flag_writes_structured_attempt_lines(self) -> None:
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["run", str(helpers.EXAMPLES / "gate_schedule.json"), "--log"])
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in err.getvalue().splitlines()]
        self.assertEqual([line["decision"] for line in lines], ["retry", "stop"])
        self.assertEqual(lines[0]["token"], "BAY-1001.0")
        self.assertEqual(lines[0]["error_type"], "TransientGateError")
        self.assertEqual(lines[0]["delay_s"], 0.25)
        self.assertEqual(lines[1]["attempt"], 1)
        self.assertEqual(json.loads(out.getvalue())["commits"], 1)
        self.assertEqual(helpers.LOGGER.level, 0)

    def test_timer_demo_starts_at_one_second(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buffer = StringIO()
        with redirect_stdout(buffer):
            code = main(["timer"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["profile"], "tcp_6298")
        self.assertEqual(payload["floor_s"], 1.0)
        self.assertGreaterEqual(payload["ceiling_s"], 60.0)
        self.assertEqual(payload["armed_s"][0], 1.0)
        self.assertEqual(payload["armed_s"][-1], 60.0)

    def test_trace_demo_prints_the_locked_counts(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buffer = StringIO()
        with redirect_stdout(buffer):
            code = main(["trace", str(helpers.EXAMPLES / "correlated_trace.json")])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["no-retry"]["successes"], 5)
        self.assertEqual(payload["standard"]["successes"], 3)
        self.assertEqual(payload["budgeted"]["successes"], 6)
        self.assertLess(payload["budgeted"]["raf"], payload["standard"]["raf"])


if __name__ == "__main__":
    unittest.main()
