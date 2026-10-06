"""Run the session-fee sheet lab offline.

From the repository root:

    python projects/sheets-gas-automation-lab/run_lab.py --dry-run
    python projects/sheets-gas-automation-lab/run_lab.py --measure
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sessionfee.fixtures import decimal_field, load_json, sessions_from_document  # noqa: E402
from sessionfee.idempotency import Ledger  # noqa: E402
from sessionfee.lockstub import LockStub  # noqa: E402
from sessionfee.measure import run_controls  # noqa: E402
from sessionfee.observe import RunLog  # noqa: E402
from sessionfee.plan import MemorySheet  # noqa: E402
from sessionfee.commit import commit_week  # noqa: E402


def run_dry() -> dict[str, object]:
    document = load_json("week_sessions.json")
    report = commit_week(
        MemorySheet(row_version=0),
        Ledger(),
        LockStub(),
        sessions_from_document(document),
        expected_total=decimal_field(document, "expected_total_jpy"),
        expected_row_count=int(document["expected_row_count"]),
        read_version=0,
        dry_run=True,
        log=RunLog(),
    )
    return {
        "outcome": report.outcome,
        "dry_run": report.dry_run,
        "formula": report.formula,
        "written_total": report.written_total,
        "row_version": report.row_version,
        "expected_total_jpy": str(Decimal(document["expected_total_jpy"])),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline session-fee sheet lab")
    parser.add_argument("--measure", action="store_true", help="Print baseline and control counts")
    parser.add_argument("--dry-run", action="store_true", help="Validate the sample week without writing")
    args = parser.parse_args(argv)
    if not args.measure and not args.dry_run:
        args.dry_run = True
        args.measure = True
    if args.dry_run:
        print(json.dumps({"dry_run": run_dry()}, indent=2, sort_keys=True))
    if args.measure:
        print(json.dumps({"controls": run_controls()}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
