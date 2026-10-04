"""CLI entry for the offline automation job runner lab."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from .checkpoint import FileCheckpointStore
from .errors import LabError, SchemaError, SimulatedCrash
from .handlers import Fault, FaultInjector
from .ledger import ExecutionLedger
from .lease import LeaseStore
from .models import CRASH_AT, LAB_EPOCH_MS
from .runner import JobRunner
from .schema import load_catalog, parse_json_text
from .seed import build_catalog, build_fault_script, build_inbox
from .store import Workspace
from .telemetry import JsonLogger, ManualClock, RecordingSleeper


class LabInputError(Exception):
    """Raised when example/input files are missing or malformed."""


def _int_in_range(low: int, high: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from exc
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local automation job runner lab (offline, synthetic workspace).",
    )
    parser.add_argument("--catalog", type=Path, help="JSON object describing the job catalog")
    parser.add_argument("--inbox", type=Path, help="JSON array of synthetic inbox records")
    parser.add_argument("--faults", type=Path, help="JSON array of one-shot handler faults")
    parser.add_argument("--state-dir", type=Path, help="Directory for durable JSON state")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-walk jobs even if the window is complete")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--crash-after-jobs", type=_int_in_range(1, 100), default=None)
    parser.add_argument("--crash-at", choices=CRASH_AT, default="post_handler")
    parser.add_argument("--until-windows", type=_int_in_range(1, 24), default=1)
    parser.add_argument("--now-ms", type=_int_in_range(0, 4_102_444_800_000), default=LAB_EPOCH_MS)
    parser.add_argument("--print-events", action="store_true", help="Include structured events in the JSON report")
    parser.add_argument("--log-jsonl", action="store_true", help="Stream structured events to stderr as JSON lines")
    return parser


def default_examples_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "examples"


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LabInputError(f"could not load {path}: {type(exc).__name__}: {exc}") from exc
    try:
        return parse_json_text(text)
    except SchemaError as exc:
        raise LabInputError(f"could not load {path}: {exc.message}") from exc


def _load_or_default(path: Optional[Path], default_name: str, builder) -> Any:
    if path is None:
        candidate = default_examples_dir() / default_name
        if candidate.exists():
            path = candidate
        else:
            return builder()
    return _load_json(path)


def _check_inbox(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise LabInputError("inbox must contain a JSON array")
    checked: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise LabInputError(f"inbox[{index}] must be an object")
        if not isinstance(row.get("id"), str) or not row["id"].strip():
            raise LabInputError(f"inbox[{index}] needs a string id")
        checked.append(row)
    return checked


def _build_faults(faults: Any) -> list[Fault]:
    if not isinstance(faults, list):
        raise LabInputError("faults must contain a JSON array")
    built: list[Fault] = []
    for index, item in enumerate(faults):
        if not isinstance(item, dict):
            raise LabInputError(f"faults[{index}] must be an object")
        try:
            built.append(Fault.from_dict(item))
        except ValueError as exc:
            raise LabInputError(f"faults[{index}]: {exc}") from exc
    return built


def run_lab(argv: Optional[list[str]] = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    catalog_raw = _load_or_default(args.catalog, "catalog.json", build_catalog)
    inbox_raw = _load_or_default(args.inbox, "inbox.json", lambda: build_inbox(window=0))
    faults_raw = _load_or_default(args.faults, "fault_script.json", build_fault_script)
    if not isinstance(catalog_raw, dict):
        raise LabInputError("catalog must contain a JSON object")
    try:
        catalog = load_catalog(catalog_raw)
    except SchemaError as exc:
        raise LabInputError(f"catalog is invalid: {exc.message}") from exc
    inbox = _check_inbox(inbox_raw)
    faults = _build_faults(faults_raw)

    dry_run = bool(args.dry_run)
    # Dry-run keeps everything in memory, so it never touches or creates a state dir.
    state_dir: Optional[Path] = None
    if not dry_run:
        state_dir = args.state_dir
        if state_dir is None:
            state_dir = Path(tempfile.mkdtemp(prefix="job-lab-"))

    clock = ManualClock(args.now_ms)
    logger = JsonLogger(clock=clock, stream=sys.stderr if args.log_jsonl else None)
    sleeper = RecordingSleeper(clock)
    workspace = Workspace(None if state_dir is None else state_dir / "workspace.json")
    if state_dir is None or not (state_dir / "workspace.json").exists():
        for row in inbox:
            workspace.upsert("inbox", row)
    ledger = ExecutionLedger(None if state_dir is None else state_dir / "ledger.json")
    leases = LeaseStore(None if state_dir is None else state_dir / "leases.json")
    store = None if state_dir is None else FileCheckpointStore(state_dir / "checkpoints")
    runner = JobRunner(
        catalog,
        workspace=workspace,
        ledger=ledger,
        store=store,
        leases=leases,
        logger=logger,
        clock=clock,
        sleeper=sleeper,
        faults=FaultInjector(faults),
        seed=7,
        crash_after_jobs=args.crash_after_jobs,
        crash_at=args.crash_at,
        fail_fast=args.fail_fast,
    )

    using_default_inbox = args.inbox is None
    reports = []
    for index in range(args.until_windows):
        if index > 0:
            clock.advance_ms(catalog.window_ms)
            if using_default_inbox:
                for row in build_inbox(window=index):
                    workspace.upsert("inbox", row)
        reports.append(
            runner.run(
                dry_run=dry_run,
                resume=not args.no_resume,
                force=args.force,
            )
        )

    payload = reports[-1].to_dict()
    payload["windows_run"] = len(reports)
    payload["failed_windows"] = sum(1 for item in reports if item.status == "failed")
    payload["state_dir"] = None if state_dir is None else str(state_dir)
    payload["workspace"] = workspace.counts()
    payload["ledger_size"] = len(ledger)
    payload["sleep_delays_ms"] = list(sleeper.delays)
    payload["window_ms"] = catalog.window_ms
    if len(reports) > 1:
        payload["window_reports"] = [item.to_dict() for item in reports]
    if args.print_events:
        payload["events"] = list(logger.events)
    return payload


def main(argv: Optional[list[str]] = None) -> int:
    try:
        payload = run_lab(argv)
    except LabInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SimulatedCrash as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}, indent=2))
        return 3
    except LabError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 4 if payload["failed_windows"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
