"""Path bootstrap for the wharf intake-sheet tests."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CORPUS = ROOT / "corpus"
EXAMPLES = ROOT / "examples"
LIBRARY = SRC / "wharf_sheet"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def corpus_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(CORPUS.iterdir()):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def library_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(LIBRARY.glob("*.py")))
