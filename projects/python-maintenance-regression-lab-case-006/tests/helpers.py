"""Path bootstrap and the checked-in CSV oracle. Synthetic data only."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXAMPLES = ROOT / "examples"
GOLDEN_PATH = ROOT / "tests" / "golden" / "rows.csv"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

GOLDEN_BYTES = b"caf\xc3\xa9,\xce\xa9\r\nx,y\r\n"
GOLDEN_SHA256 = "6eba8588c5f2f573229b74003b682de76fb6b18baa473d3096bcae9d4efab653"
GOLDEN_ROWS = [["caf\u00e9", "\u03a9"], ["x", "y"]]
MIXED_NEWLINES = b"\x61\x0d\x0a\x62\x0a\x63\x0d\x64"

LIBRARY = SRC / "netunicode_lab"


def library_sources():
    return sorted(LIBRARY.glob("*.py"))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LabTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
