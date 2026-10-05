"""Run the partner-note interchange lab from the project directory."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from netunicode_lab.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
