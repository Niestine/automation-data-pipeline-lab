"""Command line for the Kilnline repair-gate lab."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .harness import GoldJoinError
from .suite import EXAMPLES, execute_suite, render_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Kilnline structured-output repair lab.")
    parser.add_argument("--examples", type=Path, default=EXAMPLES)
    parser.add_argument("--gold", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    gold_path = args.gold if args.gold is not None else args.examples / "gold.json"
    try:
        outcome = execute_suite(
            args.examples,
            gold_path,
            args.manifest,
            write_manifest=not args.dry_run,
        )
    except GoldJoinError as exc:
        print(f"gold join failed: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = outcome["report"]
    if args.dry_run:
        print("dry_run=true")
    sys.stdout.write(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
