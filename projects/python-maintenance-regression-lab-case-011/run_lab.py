"""Run the yield lab from the project directory."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from yieldlab.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
