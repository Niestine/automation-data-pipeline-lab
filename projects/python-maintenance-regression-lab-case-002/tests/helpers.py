"""Path bootstrap for unittest modules."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXAMPLES = ROOT / "examples"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
_current = os.environ.get("PYTHONPATH", "")
_parts = [part for part in _current.split(os.pathsep) if part]
if str(SRC) not in _parts:
    os.environ["PYTHONPATH"] = str(SRC) + (os.pathsep + _current if _current else "")
