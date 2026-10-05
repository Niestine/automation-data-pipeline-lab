"""Run batchnote with this project's src directory on sys.path."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

if __name__ == "__main__":
    runpy.run_module("batchnote", run_name="__main__", alter_sys=True)
