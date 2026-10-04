"""Run the SLIP maintenance lab without installing the package."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
current = os.environ.get("PYTHONPATH", "")
parts = [part for part in current.split(os.pathsep) if part]
if str(SRC) not in parts:
    os.environ["PYTHONPATH"] = str(SRC) + (os.pathsep + current if current else "")

from slip_lab.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
