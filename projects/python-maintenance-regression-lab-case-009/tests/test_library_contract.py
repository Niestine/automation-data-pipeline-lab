"""In-process contract: run() returns a status and writes the injected streams."""

import io
import json
import logging
import os
import sys
import unittest

import helpers
from batchnote.problems import Problem, consume, render_human
from batchnote.run import REJECT_STAMP, SUGGEST_ON_ERROR, run


def _loads_lines(text):
    if text == "":
        return []
    return [json.loads(line) for line in text.splitlines()]


class LibraryContractTests(unittest.TestCase):
    def test_bogus_returns_instead_of_exiting(self):
        real_out = sys.stdout
        real_err = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        out = io.StringIO()
        err = io.StringIO()
        try:
            code = run(["--bogus"], stdout=out, stderr=err)
            leaked_out = sys.stdout.getvalue()
            leaked_err = sys.stderr.getvalue()
        finally:
            sys.stdout = real_out
            sys.stderr = real_err
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(leaked_out, "")
        self.assertEqual(leaked_err, "")
        text = err.getvalue()
        self.assertIn("usage: batchnote", text)
        self.assertIn("unrecognized arguments: --bogus", text)
        self.assertNotIn("Traceback", text)
        problem = [line for line in text.splitlines() if line.startswith("batchnote: ")]
        self.assertTrue(problem)
        for line in problem:
            message = line.split(": ", 1)[1]
            self.assertTrue(message[:1].islower())
            self.assertFalse(message.endswith("."))

    def test_choice_typo_stays_status_2(self):
        out = io.StringIO()
        err = io.StringIO()
        code = run(["--report", "huma", "note.txt"], stdout=out, stderr=err)
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        text = err.getvalue()
        self.assertIn("invalid choice", text)
        self.assertIn("usage: batchnote", text)
        if SUGGEST_ON_ERROR:
            self.assertIn("maybe you meant", text)
            self.assertTrue("human" in text or "json" in text)

    def test_renderer_keeps_a_choice_hint(self):
        problem = Problem(
            "usage",
            "argument --report: invalid choice: 'huma', maybe you meant 'human'? "
            "(choose from human, json)",
        )
        text = render_human(problem, "batchnote")
        self.assertIn("maybe you meant", text)
        self.assertIn("human", text)
        self.assertNotEqual(problem.detail, problem.title)
        self.assertFalse(problem.detail.endswith("."))
        self.assertTrue(problem.detail[:1].islower())

    def test_unknown_long_option_suggests_one_candidate(self):
        out = io.StringIO()
        err = io.StringIO()
        opened = []

        def opener(path):
            opened.append(path)
            return io.BytesIO(b"note\n")

        code = run(["--verbos", "note.txt"], stdout=out, stderr=err, opener=opener)
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(opened, [])
        self.assertIn("batchnote: closest match: --verbose", err.getvalue())
        self.assertNotIn("batchnote: closest match: --version", err.getvalue())

    def test_bogus_does_not_suggest_verbose(self):
        err = io.StringIO()
        code = run(["--bogus"], stdout=io.StringIO(), stderr=err)
        self.assertEqual(code, 2)
        self.assertNotIn("closest match:", err.getvalue())

    def test_verbos_json_keeps_status_2_and_suggestion_member(self):
        err = io.StringIO()
        code = run(
            ["--report", "json", "--verbos"],
            stdout=io.StringIO(),
            stderr=err,
        )
        self.assertEqual(code, 2)
        obj = json.loads(err.getvalue())
        self.assertEqual(obj["type"], "urn:batchnote:problem:usage")
        self.assertEqual(obj["title"], "Command line usage error")
        self.assertEqual(obj["status"], 2)
        self.assertIs(type(obj["status"]), int)
        self.assertEqual(obj["suggestion"], "--verbose")
        self.assertNotEqual(obj["detail"], obj["title"])

    def test_success_record_round_trips(self):
        raw = b"Dock notice\nLane 4 is clear\n"
        out = io.StringIO()
        err = io.StringIO()

        def opener(path):
            self.assertEqual(path, "ready.txt")
            return io.BytesIO(raw)

        code = run(["ready.txt"], stdout=out, stderr=err, opener=opener)
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        line = out.getvalue()
        self.assertTrue(line.endswith("\n"))
        self.assertNotIn("\r", line)
        obj = json.loads(line)
        self.assertEqual(set(obj), {"path", "bytes", "text"})
        self.assertEqual(obj["path"], "ready.txt")
        self.assertEqual(obj["bytes"], len(raw))
        self.assertEqual(obj["text"].encode("utf-8", "surrogateescape"), raw)

    def test_second_run_matches_the_first(self):
        raw = b"same\n"

        def opener(path):
            return io.BytesIO(raw)

        first = io.StringIO()
        second = io.StringIO()
        self.assertEqual(run(["a"], stdout=first, stderr=io.StringIO(), opener=opener), 0)
        self.assertEqual(run(["a"], stdout=second, stderr=io.StringIO(), opener=opener), 0)
        self.assertEqual(first.getvalue(), second.getvalue())

    def test_nul_and_high_byte_round_trip(self):
        samples = {
            "null_byte.bin": b"note\x00ok\n",
            "high_byte.bin": b"\xff",
        }
        for name, raw in samples.items():
            out = io.StringIO()
            err = io.StringIO()
            code = run(
                [name],
                stdout=out,
                stderr=err,
                opener=lambda _path, payload=raw: io.BytesIO(payload),
            )
            self.assertEqual(code, 0, name)
            self.assertEqual(err.getvalue(), "", name)
            obj = json.loads(out.getvalue())
            self.assertEqual(obj["bytes"], len(raw))
            self.assertEqual(obj["text"].encode("utf-8", "surrogateescape"), raw)
            if b"\x00" in raw:
                self.assertIn("\\u0000", out.getvalue())
            if raw == b"\xff":
                self.assertIn("\\udcff", out.getvalue())

    def test_partial_success_continues(self):
        files = {
            "a.txt": b"aaa",
            "missing.txt": FileNotFoundError(2, "No such file or directory", "missing.txt"),
            "b.txt": b"bb",
        }
        opened = []

        def opener(path):
            opened.append(path)
            item = files[path]
            if isinstance(item, BaseException):
                raise item
            return io.BytesIO(item)

        out = io.StringIO()
        err = io.StringIO()
        code = run(["a.txt", "missing.txt", "b.txt"], stdout=out, stderr=err, opener=opener)
        self.assertEqual(code, 1)
        self.assertEqual(opened, ["a.txt", "missing.txt", "b.txt"])
        rows = _loads_lines(out.getvalue())
        self.assertEqual([row["path"] for row in rows], ["a.txt", "b.txt"])
        self.assertEqual([row["bytes"] for row in rows], [3, 2])
        self.assertIn("missing.txt", err.getvalue())
        self.assertIn("No such file or directory", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_titles_match_across_files_and_details_do_not(self):
        def opener(path):
            raise FileNotFoundError(2, "No such file or directory", path)

        err = io.StringIO()
        code = run(
            ["--report", "json", "one.txt", "two.txt"],
            stdout=io.StringIO(),
            stderr=err,
            opener=opener,
        )
        self.assertEqual(code, 1)
        rows = _loads_lines(err.getvalue())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], rows[1]["title"])
        self.assertEqual(rows[0]["title"], "Input unavailable")
        self.assertNotEqual(rows[0]["detail"], rows[1]["detail"])
        self.assertTrue(all(row["status"] == code for row in rows))
        self.assertTrue(all(type(row["status"]) is int for row in rows))

    def test_permission_is_recoverable_and_directory_is_io(self):
        def opener(path):
            if path == "locked.txt":
                raise PermissionError(13, "Permission denied", path)
            if path == "dir":
                raise IsADirectoryError(21, "Is a directory", path)
            return io.BytesIO(b"ok")

        err = io.StringIO()
        out = io.StringIO()
        code = run(
            ["--report", "json", "locked.txt", "dir", "ok.txt"],
            stdout=out,
            stderr=err,
            opener=opener,
        )
        self.assertEqual(code, 1)
        rows = _loads_lines(err.getvalue())
        self.assertEqual([row["type"] for row in rows], [
            "urn:batchnote:problem:no-input",
            "urn:batchnote:problem:io",
        ])
        self.assertEqual(json.loads(out.getvalue())["path"], "ok.txt")

    def test_generic_oserror_stops_the_loop(self):
        opened = []

        def opener(path):
            opened.append(path)
            if path == "first":
                exc = OSError(12, "Cannot allocate memory")
                exc.filename = path
                raise exc
            return io.BytesIO(b"x")

        err = io.StringIO()
        code = run(
            ["--report", "json", "first", "second"],
            stdout=io.StringIO(),
            stderr=err,
            opener=opener,
        )
        self.assertEqual(code, 1)
        self.assertEqual(opened, ["first"])
        obj = json.loads(err.getvalue())
        self.assertEqual(obj["type"], "urn:batchnote:problem:os")
        self.assertEqual(obj["status"], 1)
        self.assertIn("first", obj["detail"])
        self.assertIn("Cannot allocate memory", obj["detail"])

    def test_stdout_oserror_stops_and_keeps_whole_lines(self):
        class Boom(io.StringIO):
            def __init__(self):
                super().__init__()
                self.writes = 0

            def write(self, text):
                self.writes += 1
                if self.writes >= 2:
                    raise OSError(28, "No space left on device")
                return super().write(text)

        opened = []

        def opener(path):
            opened.append(path)
            return io.BytesIO(path.encode("ascii"))

        out = Boom()
        err = io.StringIO()
        code = run(
            ["--report", "json", "a", "b", "c"],
            stdout=out,
            stderr=err,
            opener=opener,
        )
        self.assertEqual(code, 1)
        self.assertEqual(opened, ["a", "b"])
        written = _loads_lines(out.getvalue())
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["path"], "a")
        self.assertTrue(out.getvalue().endswith("\n"))
        obj = json.loads(err.getvalue())
        self.assertEqual(obj["type"], "urn:batchnote:problem:io")
        self.assertEqual(obj["status"], code)
        self.assertIn("No space left on device", obj["detail"])

    def test_stderr_oserror_returns_failure(self):
        class Dead(io.StringIO):
            def write(self, text):
                raise OSError(5, "Input/output error")

        code = run(["--bogus"], stdout=io.StringIO(), stderr=Dead())
        self.assertEqual(code, 1)

    def test_dead_stderr_with_verbose_does_not_leak_a_logging_traceback(self):
        class Dead(io.StringIO):
            def write(self, text):
                raise OSError(5, "Input/output error")

        real_err = sys.stderr
        sys.stderr = io.StringIO()
        out = io.StringIO()
        try:
            code = run(
                ["--verbose", "a"],
                stdout=out,
                stderr=Dead(),
                opener=lambda _path: io.BytesIO(b"abc"),
            )
            leaked = sys.stderr.getvalue()
        finally:
            sys.stderr = real_err
        self.assertEqual(code, 0)
        self.assertEqual(leaked, "")
        self.assertEqual(json.loads(out.getvalue())["path"], "a")

    def test_argv_index_after_end_of_options(self):
        def opener(path):
            raise FileNotFoundError(2, "No such file or directory", path)

        err = io.StringIO()
        code = run(
            ["--report", "json", "--", "--config", "x"],
            stdout=io.StringIO(),
            stderr=err,
            opener=opener,
        )
        self.assertEqual(code, 1)
        rows = _loads_lines(err.getvalue())
        self.assertEqual(
            [(row["locator"]["path"], row["locator"]["argv_index"]) for row in rows],
            [("--config", 3), ("x", 4)],
        )

    def test_reject_stamp_is_recoverable_data(self):
        good = b"Dock notice\nLane 4 is clear\n"
        bad = b"Dock notice\n\t[[reject]] hold this note\n"
        opened = []

        def opener(path):
            opened.append(path)
            return io.BytesIO(bad if path == "hold.txt" else good)

        out = io.StringIO()
        err = io.StringIO()
        code = run(
            ["--report", "json", "ready.txt", "hold.txt", "second.txt"],
            stdout=out,
            stderr=err,
            opener=opener,
        )
        self.assertEqual(code, 1)
        self.assertEqual(opened, ["ready.txt", "hold.txt", "second.txt"])
        self.assertEqual(
            [row["path"] for row in _loads_lines(out.getvalue())],
            ["ready.txt", "second.txt"],
        )
        obj = json.loads(err.getvalue())
        self.assertEqual(obj["type"], "urn:batchnote:problem:data")
        self.assertEqual(obj["title"], "Input data error")
        self.assertEqual(obj["status"], 1)
        self.assertEqual(obj["detail"], "rejected note stamp")
        self.assertEqual(obj["locator"]["line"], 2)
        self.assertEqual(obj["locator"]["column"], 9)
        self.assertIn(REJECT_STAMP, bad.decode("utf-8"))

    def test_config_gate_does_not_change_the_record(self):
        blobs = {
            "batch.json": b'{"batch": "lane-4"}\n',
            "note.txt": b"hello\n",
        }

        def opener(path):
            return io.BytesIO(blobs[path])

        plain = io.StringIO()
        gated = io.StringIO()
        self.assertEqual(run(["note.txt"], stdout=plain, stderr=io.StringIO(), opener=opener), 0)
        self.assertEqual(
            run(
                ["--config", "batch.json", "note.txt"],
                stdout=gated,
                stderr=io.StringIO(),
                opener=opener,
            ),
            0,
        )
        self.assertEqual(plain.getvalue(), gated.getvalue())
        self.assertNotIn("lane-4", gated.getvalue())

    def test_bad_config_does_not_open_operands(self):
        opened = []

        def opener(path):
            opened.append(path)
            if path == "broken.json":
                return io.BytesIO(b"{")
            if path == "text.json":
                return io.BytesIO(b"\xff")
            if path == "shape.json":
                return io.BytesIO(b'{"note": "missing batch"}')
            if path == "missing.json":
                raise FileNotFoundError(2, "No such file or directory", path)
            return io.BytesIO(b"should-not-run")

        cases = ["broken.json", "text.json", "shape.json", "missing.json"]
        for name in cases:
            opened.clear()
            err = io.StringIO()
            out = io.StringIO()
            code = run(
                ["--report", "json", "--config", name, "note.txt"],
                stdout=out,
                stderr=err,
                opener=opener,
            )
            self.assertEqual(code, 1, name)
            self.assertEqual(out.getvalue(), "", name)
            self.assertEqual(opened, [name], name)
            obj = json.loads(err.getvalue())
            self.assertEqual(obj["type"], "urn:batchnote:problem:config", name)
            self.assertEqual(obj["status"], 1, name)

    def test_end_of_options_and_literal_dash(self):
        opened = []

        def opener(path):
            opened.append(path)
            return io.BytesIO(b"x")

        code = run(["--", "--help"], stdout=io.StringIO(), stderr=io.StringIO(), opener=opener)
        self.assertEqual(code, 0)
        self.assertEqual(opened, ["--help"])
        opened.clear()
        code = run(["-"], stdout=io.StringIO(), stderr=io.StringIO(), opener=opener)
        self.assertEqual(code, 0)
        self.assertEqual(opened, ["-"])

    def test_help_and_version_do_not_open(self):
        opened = []

        def opener(path):
            opened.append(path)
            return io.BytesIO(b"x")

        out = io.StringIO()
        self.assertEqual(run(["--bogus", "--help"], stdout=out, stderr=io.StringIO(), opener=opener), 0)
        self.assertIn("Public statuses:", out.getvalue())
        self.assertEqual(opened, [])
        out = io.StringIO()
        err = io.StringIO()
        self.assertEqual(run(["--version"], stdout=out, stderr=err), 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(out.getvalue(), "batchnote 1.0.0\n")
        err = io.StringIO()
        self.assertEqual(
            run(["--report", "huma", "--help"], stdout=io.StringIO(), stderr=err, opener=opener),
            2,
        )
        self.assertIn("invalid choice", err.getvalue())

    def test_verbose_lines_are_not_problems_and_do_not_leak(self):
        def opener(path):
            return io.BytesIO(b"abc")

        err1 = io.StringIO()
        err2 = io.StringIO()
        quiet = io.StringIO()
        self.assertEqual(run(["--verbose", "a"], stdout=io.StringIO(), stderr=err1, opener=opener), 0)
        self.assertEqual(run(["--verbose", "a"], stdout=io.StringIO(), stderr=err2, opener=opener), 0)
        self.assertEqual(run(["a"], stdout=io.StringIO(), stderr=quiet, opener=opener), 0)
        self.assertEqual(err1.getvalue().count("batchnote: debug:"), err2.getvalue().count("batchnote: debug:"))
        self.assertIn("batchnote: debug: reading a", err1.getvalue())
        self.assertNotIn("urn:batchnote", err1.getvalue())
        self.assertEqual(quiet.getvalue(), "")
        root_records = []

        class Grab(logging.Handler):
            def emit(self, record):
                root_records.append(record)

        root = logging.getLogger()
        grab = Grab()
        root.addHandler(grab)
        try:
            run(["--verbose", "a"], stdout=io.StringIO(), stderr=io.StringIO(), opener=opener)
        finally:
            root.removeHandler(grab)
        self.assertEqual(root_records, [])

    def test_software_traceback_requires_debug(self):
        def opener(path):
            raise RuntimeError("invariant broken")

        previous = os.environ.pop("BATCHNOTE_DEBUG", None)
        try:
            err = io.StringIO()
            code = run(
                ["--report", "json", "note.txt"],
                stdout=io.StringIO(),
                stderr=err,
                opener=opener,
            )
            self.assertEqual(code, 1)
            text = err.getvalue()
            self.assertNotIn("Traceback", text)
            self.assertNotIn('File "', text)
            self.assertNotIn("invariant broken", text)
            self.assertNotIn("RuntimeError", text)
            obj = json.loads(text)
            self.assertEqual(obj["type"], "urn:batchnote:problem:software")
            self.assertEqual(obj["status"], 1)
            self.assertEqual(obj["detail"], "internal invariant failed")
            self.assertFalse(set(obj) - {"type", "title", "status", "detail", "locator", "suggestion"})

            os.environ["BATCHNOTE_DEBUG"] = "1"
            err = io.StringIO()
            code = run(["note.txt"], stdout=io.StringIO(), stderr=err, opener=opener)
            self.assertEqual(code, 1)
            self.assertIn("Traceback (most recent call last)", err.getvalue())
            self.assertIn("internal invariant failed", err.getvalue())
            self.assertIn("invariant broken", err.getvalue())
        finally:
            os.environ.pop("BATCHNOTE_DEBUG", None)
            if previous is not None:
                os.environ["BATCHNOTE_DEBUG"] = previous

    def test_keyboard_interrupt_and_system_exit_propagate(self):
        def interrupt(path):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            run(["x"], stdout=io.StringIO(), stderr=io.StringIO(), opener=interrupt)

        def leave(path):
            raise SystemExit(7)

        with self.assertRaises(SystemExit) as caught:
            run(["x"], stdout=io.StringIO(), stderr=io.StringIO(), opener=leave)
        self.assertEqual(caught.exception.code, 7)

    def test_consumer_ignores_unknown_members_and_detail(self):
        fixture = json.loads(
            (helpers.EXAMPLES / "vendor_note_problem.json").read_text(encoding="utf-8")
        )
        self.assertIn("vendorNote", fixture)
        first = consume(fixture)
        second = consume({
            "type": fixture["type"],
            "detail": "a completely different sentence",
            "vendorNote": "still ignored",
        })
        future = consume({"type": "urn:batchnote:problem:future", "vendorNote": "x"})
        self.assertEqual(first, second)
        self.assertEqual(first, "urn:batchnote:problem:no-input")
        self.assertEqual(future, "urn:batchnote:problem:future")
        with self.assertRaises(ValueError):
            consume({"detail": "no type"})
        with self.assertRaises(ValueError):
            consume({"type": "about:blank"})

    def test_detail_cannot_reuse_the_title(self):
        with self.assertRaises(ValueError):
            Problem("usage", "Command line usage error.")

    def test_human_grammar_for_a_located_message(self):
        problem = Problem(
            "data",
            "Rejected note stamp.",
            locator={"path": "hold.txt", "line": 2, "column": 9},
        )
        self.assertEqual(
            render_human(problem, "batchnote"),
            "batchnote: hold.txt:2:9: rejected note stamp\n",
        )
        self.assertNotEqual(problem.detail, problem.title)


if __name__ == "__main__":
    unittest.main()
