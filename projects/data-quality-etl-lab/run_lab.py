"""Run the harbor-ledger intake on one synthetic supplier drop."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from harbor_ledger import load_profile, load_schema, run_path  # noqa: E402
from harbor_ledger.errors import HarborError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seal a supplier CSV into a canonical catalog and an anomaly report.")
    parser.add_argument("--input", required=True, help="Supplier CSV bytes")
    parser.add_argument("--schema", required=True, help="Table schema JSON")
    parser.add_argument("--profile", required=True, help="Frozen detector and dedup profile JSON")
    parser.add_argument("--out-dir", required=True, help="Directory for clean.csv, report.jcs, and run_manifest.json")
    parser.add_argument("--dry-run", action="store_true", help="Compute the digest and write nothing")
    parser.add_argument("--owned", action="store_true", help="Treat the input as a pipeline-owned UTF-8 file")
    parser.add_argument("--oracle", help="Optional cell-oracle JSON for per-class metrics")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging()
    try:
        schema = load_schema(args.schema)
        profile = load_profile(args.profile)
        oracle = None
        if args.oracle:
            payload = json.loads(Path(args.oracle).read_text(encoding="utf-8"))
            oracle = payload["cells"] if isinstance(payload, dict) else payload
        result = run_path(
            args.input,
            schema,
            profile,
            out_dir=args.out_dir,
            dry_run=args.dry_run,
            owned=args.owned,
            oracle=oracle,
        )
    except (OSError, HarborError, json.JSONDecodeError, KeyError) as exc:
        print(f"harbor-ledger: {exc}", file=sys.stderr)
        return 2
    if result.status == "fatal":
        print(f"harbor-ledger: {result.error}", file=sys.stderr)
        return 2
    print(f"status={result.status} digest={result.digest}")
    return 0


def _configure_logging() -> None:
    logger = logging.getLogger("harbor_ledger")
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


if __name__ == "__main__":
    sys.exit(main())
