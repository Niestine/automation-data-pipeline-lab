"""Run the tide-desk lab from the repository root or this directory."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from tide_route.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
