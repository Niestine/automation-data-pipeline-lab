"""Shared paths and process-image helpers for the batchnote suite."""

import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PROJECT = TESTS.parent
SRC = PROJECT / "src"
REPO = PROJECT.parent.parent

if REPO.joinpath("projects", PROJECT.name) != PROJECT:
    raise RuntimeError("could not locate the repository root")

sys.path.insert(0, str(SRC))

EXAMPLES = PROJECT / "examples"


def assert_stderr_clean(blob: bytes) -> None:
    text = blob.decode("utf-8", "replace")
    if "Traceback" in text or 'File "' in text:
        raise AssertionError(text)


def _child_env(extra=None, force_utf8=True):
    env = os.environ.copy()
    env.pop("BATCHNOTE_DEBUG", None)
    env["PYTHONPATH"] = str(SRC)
    if force_utf8:
        env["PYTHONIOENCODING"] = "utf-8"
    else:
        # Locale mode: the child picks its own stream encoding.
        env.pop("PYTHONIOENCODING", None)
        env["PYTHONUTF8"] = "0"
    if extra:
        env.update(extra)
    return env


def run_cli(args, env_extra=None, cwd=None, force_utf8=True):
    proc = subprocess.run(
        [sys.executable, "-m", "batchnote", *args],
        cwd=str(REPO if cwd is None else cwd),
        capture_output=True,
        env=_child_env(env_extra, force_utf8),
        check=False,
    )
    if proc.returncode not in (0, 1, 2):
        raise AssertionError(proc.returncode)
    assert_stderr_clean(proc.stderr)
    return proc


def run_lab(args):
    env = os.environ.copy()
    env.pop("BATCHNOTE_DEBUG", None)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(PROJECT / "run_lab.py"), *args],
        cwd=str(REPO),
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.returncode not in (0, 1, 2):
        raise AssertionError(proc.returncode)
    assert_stderr_clean(proc.stderr)
    return proc
