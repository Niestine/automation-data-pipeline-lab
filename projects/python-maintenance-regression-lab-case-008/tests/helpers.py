"""Path setup shared by the case 008 tests."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def capture_logger():
    log = logging.getLogger("prefixlab")
    log.setLevel(logging.INFO)
    handler = logging.Handler()
    lines: list[str] = []

    def emit(record):
        lines.append(record.getMessage())

    handler.emit = emit
    log.addHandler(handler)
    return log, handler, lines
