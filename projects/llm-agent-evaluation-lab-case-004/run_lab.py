"""Run the office-gate lab from the repository root or this directory."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from office_gate_lab.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
