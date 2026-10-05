"""CLI for the offline quay release inbox."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .lab import project_root, run_demo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quay-inbox")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--examples", type=Path, default=None)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    examples = args.examples or (project_root() / "examples")
    try:
        report = run_demo(examples, dry_run=args.dry_run, state=args.state, seed=args.seed)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
        print(exc.__class__.__name__, file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
