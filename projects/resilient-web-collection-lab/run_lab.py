"""Run the offline resilient web collection lab without installing the package."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from web_collection_lab.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
