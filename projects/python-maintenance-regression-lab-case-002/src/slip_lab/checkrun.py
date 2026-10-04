"""Read-only corpus check: dispositions, metamorphic relations, branches."""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from slip_lab import engine
from slip_lab.dialect import is_records
from slip_lab.differential import classify_inprocess
from slip_lab.errors import LabError
from slip_lab.gate import evaluate
from slip_lab.hardened import parse as hardened_parse
from slip_lab.legacy import parse as legacy_parse
from slip_lab.logging_setup import log_case
from slip_lab.metamorphic import check_quote_round_trip, check_success
from slip_lab.model import PRIORITY
from slip_lab.paths import CORPUS
from slip_lab.stock import SPECS, build_rows, load_published


def run_check(corpus: Path = CORPUS) -> dict:
    engine.reset_branches()
    problems: list[str] = []
    try:
        disk_rows = load_published(corpus)
    except (LabError, OSError) as exc:
        problems.append(f"published corpus: {type(exc).__name__}: {exc}")
    else:
        if disk_rows != build_rows():
            problems.append("ledger file does not match the embedded corpus")
    fresh_rows = build_rows()
    by_id = {row["id"]: row for row in fresh_rows}
    classified = {}
    violations = 0
    skips = 0
    logger = logging.getLogger("slip_lab.check")
    for spec in SPECS:
        outcome = classify_inprocess(spec["blob"])
        classified[spec["id"]] = outcome
        row = by_id[spec["id"]]
        for problem in evaluate(row, outcome):
            problems.append(f"{spec['id']}: {problem}")
        log_case(
            logger,
            case_id=spec["id"],
            outcome=outcome.kind,
            side=outcome.side,
            disposition=row.get("disposition"),
            byte_length=len(spec["blob"]),
            digest=row["sha256"],
            parent_ids=list(row.get("parent_ids") or []),
            truncated=outcome.truncated,
            exc_type=outcome.legacy.exc_type or outcome.hardened.exc_type,
        )
        if outcome.kind == "AGREE" and outcome.hardened.status == "ok" and is_records(outcome.hardened.value):
            report = check_success(hardened_parse, outcome.hardened.value, full=True)
            violations += report.violations
            skips += report.skips
            if row.get("disposition") == "accept_compat":
                legacy_quote = check_quote_round_trip(legacy_parse, outcome.hardened.value)
                violations += legacy_quote.violations
                skips += legacy_quote.skips
    non_agree = [row for row in fresh_rows if row["outcome"] != "AGREE"]
    labeled = [row for row in non_agree if row.get("disposition")]
    crashes: Counter[str] = Counter()
    hangs: Counter[str] = Counter()
    for outcome in classified.values():
        if outcome.kind == "CRASH" and outcome.side:
            crashes[outcome.side] += 1
        if outcome.kind == "HANG" and outcome.side:
            hangs[outcome.side] += 1
    ordered = sorted(fresh_rows, key=lambda row: (PRIORITY[row["outcome"]], row["id"]))
    return {
        "cases": [
            {
                "disposition": row.get("disposition"),
                "id": row["id"],
                "outcome": row["outcome"],
                "side": row["side"],
            }
            for row in ordered
        ],
        "crash_by_side": {side: crashes.get(side, 0) for side in ("legacy", "hardened", "both")},
        "disposition_coverage": (len(labeled) / len(non_agree)) if non_agree else 1.0,
        "dry_run": True,
        "gate_problems": problems,
        "hang_by_side": {side: hangs.get(side, 0) for side in ("legacy", "hardened", "both")},
        "metamorphic_skips": skips,
        "metamorphic_violations": violations,
        "outcome_coverage": sum(1 for outcome in classified.values() if outcome.kind in PRIORITY) / len(SPECS),
        "untouched_branches": engine.untouched_branches(),
    }
