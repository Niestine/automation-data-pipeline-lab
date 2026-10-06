"""Command line for the Ledgerlane memory lab."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fieldlog.evaluate import build_report
from fieldlog.models import project_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Ledgerlane field-station memory lab.")
    parser.add_argument(
        "--example",
        default=str(project_dir() / "examples" / "station_shift.json"),
        help="Path to a synthetic shift file.",
    )
    args = parser.parse_args(argv)
    path = Path(args.example)
    if not path.is_file():
        print(f"example not found: {path}", file=sys.stderr)
        return 2
    try:
        report = build_report(path)
    except (ValueError, KeyError) as exc:
        print(f"invalid shift file {path}: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if report["claim_blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
