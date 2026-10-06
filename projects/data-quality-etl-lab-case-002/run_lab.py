"""Run the storeroom parts-catalog edition demo.

From the repository root:

    python projects/data-quality-etl-lab-case-002/run_lab.py --out-dir projects/data-quality-etl-lab-case-002/examples/out
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from edition_gate.pipeline import run_demo  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Edition Gate parts-catalog demo")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        result = run_demo(args.out_dir, dry_run=args.dry_run, attempts=args.attempts)
    except (OSError, ValueError) as exc:
        print("edition-gate failed: {0}".format(exc), file=sys.stderr)
        return 2
    manifest = result["manifest"]
    for batch in manifest["batches"]:
        print(
            "{0} status={1} reason={2} rows={3}".format(
                batch["name"],
                batch["status"],
                batch["reason"],
                batch["rows"],
            )
        )
    probe = manifest["dialect_probe"]
    print(
        "dialect delimiter={0} sniffer={1}".format(
            probe["delimiter"],
            probe["sniffer_delimiter"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
