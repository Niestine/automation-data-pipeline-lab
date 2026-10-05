"""The same bytes and the same digest when the child locale codec changes."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import GOLDEN_SHA256, SRC


INGEST_CHILD = textwrap.dedent(
    r"""
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, os.environ["LAB_SRC"])
    from netunicode_lab import read_foreign_text

    path = Path("sample.bin")
    path.write_bytes(bytes.fromhex("c3a9"))
    text = read_foreign_text(path)
    sys.stdout.buffer.write(str(ord(text)).encode("ascii") + b"\n")
    """
)

EMIT_CHILD = textwrap.dedent(
    r"""
    import hashlib
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, os.environ["LAB_SRC"])
    from netunicode_lab import emit_interchange_csv

    destination = Path("out.csv")
    rows = [["caf\u00e9", "\u03a9"], ["x", "y"]]
    emit_interchange_csv(destination, rows)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    sys.stdout.buffer.write(digest.encode("ascii") + b"\n")
    """
)


def run_child(code: str, *, pythonutf8: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        script = root / "child.py"
        script.write_text(code, encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["LAB_SRC"] = str(SRC)
        env["PYTHONUTF8"] = pythonutf8
        env.pop("PYTHONIOENCODING", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(script)],
            cwd=root,
            env=env,
            capture_output=True,
            check=False,
        )


class LocaleTests(unittest.TestCase):
    def test_foreign_e_acute_stays_u00e9_when_utf8_mode_is_off(self):
        for flag in ("0", "1"):
            with self.subTest(pythonutf8=flag):
                result = run_child(INGEST_CHILD, pythonutf8=flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), b"233")

    def test_emit_digest_ignores_pythonutf8_and_pythonioencoding(self):
        cases = [
            ("0", {}),
            ("1", {}),
            ("0", {"PYTHONIOENCODING": "cp1252"}),
            ("1", {"PYTHONIOENCODING": "utf-8"}),
        ]
        for flag, extra in cases:
            with self.subTest(pythonutf8=flag, extra=extra):
                result = run_child(EMIT_CHILD, pythonutf8=flag, extra_env=extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), GOLDEN_SHA256.encode("ascii"))


if __name__ == "__main__":
    unittest.main()
