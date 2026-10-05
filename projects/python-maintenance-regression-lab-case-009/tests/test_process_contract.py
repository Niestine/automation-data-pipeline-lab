"""Subprocess oracle for the batchnote process image."""

import json
import unittest

import helpers
from batchnote.problems import consume
from batchnote.run import SUGGEST_ON_ERROR


def _json_lines(blob):
    text = blob.decode("utf-8")
    if text == "":
        return []
    return [json.loads(line) for line in text.splitlines()]


class ProcessContractTests(unittest.TestCase):
    def setUp(self):
        self.ready = helpers.EXAMPLES / "ready_note.txt"
        self.second = helpers.EXAMPLES / "second_note.txt"
        self.reject = helpers.EXAMPLES / "reject_stamp.txt"
        self.empty = helpers.EXAMPLES / "empty_note.txt"
        self.nul = helpers.EXAMPLES / "null_byte.bin"
        self.high = helpers.EXAMPLES / "high_byte.bin"
        self.config = helpers.EXAMPLES / "batch_config.json"
        self.bad_config = helpers.EXAMPLES / "bad_config.json"
        self.broken_config = helpers.EXAMPLES / "broken_config.json"

    def test_one_valid_file(self):
        raw = self.ready.read_bytes()
        proc = helpers.run_cli([str(self.ready)])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        rows = _json_lines(proc.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], str(self.ready))
        self.assertEqual(rows[0]["bytes"], len(raw))
        self.assertEqual(rows[0]["text"].encode("utf-8", "surrogateescape"), raw)

    def test_help_and_version(self):
        for flag in ("--help", "-h", "--version"):
            proc = helpers.run_cli([flag])
            self.assertEqual(proc.returncode, 0, flag)
            self.assertEqual(proc.stderr, b"", flag)
            self.assertGreater(len(proc.stdout), 0, flag)
        version = helpers.run_cli(["--version"])
        self.assertEqual(version.stdout.decode("utf-8").splitlines(), ["batchnote 1.0.0"])
        help_proc = helpers.run_cli(["--help"])
        self.assertIn(b"Public statuses: 0 success, 1 failure, 2 usage.", help_proc.stdout)
        self.assertIn(b"[[reject]]", help_proc.stdout)

    def test_bogus_and_missing_operand(self):
        for args in (["--bogus"], []):
            proc = helpers.run_cli(args)
            self.assertEqual(proc.returncode, 2, args)
            self.assertEqual(proc.stdout, b"", args)
            text = proc.stderr.decode("utf-8")
            self.assertIn("usage: batchnote", text)
            problem = [line for line in text.splitlines() if line.startswith("batchnote: ")]
            self.assertTrue(problem, text)
            for line in problem:
                message = line.split(": ", 1)[1]
                self.assertTrue(message[:1].islower(), line)
                self.assertFalse(message.endswith("."), line)
        bogus = helpers.run_cli(["--bogus"])
        self.assertIn(b"unrecognized arguments: --bogus", bogus.stderr)
        self.assertNotEqual(bogus.returncode, 64)

    def test_choice_typo(self):
        proc = helpers.run_cli(["--report", "huma", str(self.ready)])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        text = proc.stderr.decode("utf-8")
        self.assertIn("invalid choice", text)
        self.assertIn("usage: batchnote", text)
        if SUGGEST_ON_ERROR:
            self.assertIn("maybe you meant", text)

    def test_verbose_typo_is_not_accepted(self):
        proc = helpers.run_cli(["--verbos", str(self.ready)])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        text = proc.stderr.decode("utf-8")
        self.assertIn("--verbose", text)
        self.assertIn("batchnote: closest match: --verbose", text)

    def test_two_valid_files_and_one_missing(self):
        missing = helpers.PROJECT / "examples" / "absent" / "middle.txt"
        proc = helpers.run_cli([str(self.ready), str(missing), str(self.second)])
        self.assertEqual(proc.returncode, 1)
        rows = _json_lines(proc.stdout)
        self.assertEqual([row["path"] for row in rows], [str(self.ready), str(self.second)])
        self.assertEqual(rows[0]["bytes"], len(self.ready.read_bytes()))
        self.assertEqual(rows[1]["bytes"], len(self.second.read_bytes()))
        text = proc.stderr.decode("utf-8")
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn(str(missing), text)

    def test_256_missing_paths_do_not_wrap_to_success(self):
        # Short relative names keep the Windows command line under the limit.
        missing = ["m%s" % index for index in range(256)]
        proc = helpers.run_cli(missing, cwd=helpers.PROJECT)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, b"")
        lines = proc.stderr.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 256)
        self.assertIn("batchnote: m0:", lines[0])
        self.assertIn("batchnote: m255:", lines[-1])

    def test_nul_file(self):
        raw = self.nul.read_bytes()
        self.assertIn(b"\x00", raw)
        proc = helpers.run_cli([str(self.nul)])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        self.assertIn(b"\\u0000", proc.stdout)
        obj = _json_lines(proc.stdout)[0]
        self.assertEqual(obj["bytes"], len(raw))
        self.assertEqual(obj["text"].encode("utf-8", "surrogateescape"), raw)

    def test_lone_high_byte(self):
        raw = self.high.read_bytes()
        self.assertEqual(raw, b"\xff")
        proc = helpers.run_cli([str(self.high)])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        obj = _json_lines(proc.stdout)[0]
        self.assertEqual(obj["text"].encode("utf-8", "surrogateescape"), raw)

    def test_empty_file(self):
        proc = helpers.run_cli([str(self.empty)])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        obj = _json_lines(proc.stdout)[0]
        self.assertEqual(obj["bytes"], 0)
        self.assertEqual(obj["text"], "")

    def test_json_report_for_a_missing_path(self):
        missing = str(helpers.PROJECT / "examples" / "absent" / "one.txt")
        proc = helpers.run_cli(["--report", "json", missing])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, b"")
        obj = json.loads(proc.stderr.decode("utf-8"))
        self.assertEqual(obj["type"], "urn:batchnote:problem:no-input")
        self.assertEqual(obj["title"], "Input unavailable")
        self.assertEqual(obj["status"], proc.returncode)
        self.assertIs(type(obj["status"]), int)
        self.assertEqual(obj["locator"]["path"], missing)
        self.assertEqual(obj["locator"]["argv_index"], 2)
        obj["vendorNote"] = "synthetic extension"
        self.assertEqual(consume(obj), obj["type"])
        del obj["detail"]
        self.assertEqual(consume(obj), "urn:batchnote:problem:no-input")

    def test_reject_stamp_on_the_process_image(self):
        raw = self.reject.read_bytes()
        line = raw.decode("utf-8").splitlines()[1]
        self.assertTrue(line.startswith("\t"))
        index = line.index("[[reject]]")
        column = 1
        for char in line[:index]:
            if char == "\t":
                column += 8 - ((column - 1) % 8)
            else:
                column += 1
        self.assertEqual(column, 9)
        proc = helpers.run_cli([str(self.ready), str(self.reject), str(self.second)])
        self.assertEqual(proc.returncode, 1)
        rows = _json_lines(proc.stdout)
        self.assertEqual([row["path"] for row in rows], [str(self.ready), str(self.second)])
        text = proc.stderr.decode("utf-8")
        self.assertIn(":%s:%s: rejected note stamp" % (2, column), text)
        self.assertIn(str(self.reject), text)

    def test_config_success_and_failure(self):
        good = helpers.run_cli(["--config", str(self.config), str(self.ready)])
        plain = helpers.run_cli([str(self.ready)])
        self.assertEqual(good.returncode, 0)
        self.assertEqual(good.stdout, plain.stdout)
        bad = helpers.run_cli(["--config", str(self.bad_config), str(self.ready)])
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(bad.stdout, b"")
        self.assertIn(str(self.bad_config).encode("utf-8"), bad.stderr)
        broken = helpers.run_cli(["--report", "json", "--config", str(self.broken_config), str(self.ready)])
        self.assertEqual(broken.returncode, 1)
        self.assertEqual(broken.stdout, b"")
        obj = json.loads(broken.stderr.decode("utf-8"))
        self.assertEqual(obj["type"], "urn:batchnote:problem:config")
        self.assertEqual(obj["status"], 1)

    def test_verbose_process_image(self):
        proc = helpers.run_cli(["--verbose", str(self.ready)])
        self.assertEqual(proc.returncode, 0)
        text = proc.stderr.decode("utf-8")
        self.assertIn("batchnote: debug: reading ", text)
        self.assertNotIn("urn:batchnote", text)
        self.assertEqual(len(_json_lines(proc.stdout)), 1)

    def test_help_beats_a_later_bogus_token(self):
        proc = helpers.run_cli(["--help", "--bogus"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        self.assertIn(b"usage:", proc.stdout)

    def test_directory_operand_is_io_on_every_platform(self):
        # Windows raises PermissionError for a directory. The contract says io.
        directory = str(helpers.EXAMPLES)
        proc = helpers.run_cli(["--report", "json", directory, str(self.ready)])
        self.assertEqual(proc.returncode, 1)
        rows = _json_lines(proc.stdout)
        self.assertEqual([row["path"] for row in rows], [str(self.ready)])
        obj = json.loads(proc.stderr.decode("utf-8"))
        self.assertEqual(obj["type"], "urn:batchnote:problem:io")
        self.assertEqual(obj["status"], proc.returncode)
        self.assertEqual(obj["locator"]["path"], directory)

    def test_option_after_operand_is_usage_and_opens_nothing(self):
        proc = helpers.run_cli([str(self.ready), "--verbose", str(self.second)])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        text = proc.stderr.decode("utf-8")
        self.assertIn("usage: batchnote", text)
        self.assertNotIn("debug:", text)

    def test_human_stderr_is_utf8_without_an_io_encoding_override(self):
        missing = "examples/absent/ノート\U0001f4c4.txt"
        proc = helpers.run_cli([missing], cwd=helpers.PROJECT, force_utf8=False)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(("batchnote: %s: " % missing).encode("utf-8"), proc.stderr)

    def test_run_lab_version(self):
        proc = helpers.run_lab(["--version"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        self.assertEqual(proc.stdout.decode("utf-8").splitlines(), ["batchnote 1.0.0"])


if __name__ == "__main__":
    unittest.main()
