"""CLI entry for the offline resilient web collection lab."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from .catalog import CatalogStore, snapshot_path_for
from .checkpoint import FileCheckpointStore, MemoryCheckpointStore
from .client import build_client
from .collector import CollectJob
from .csv_export import csv_path_for
from .errors import LabError, SimulatedCrash
from .fixture_site import Fault, FixtureSite
from .models import LAB_NOW_MS, USER_AGENT
from .retry import RetryPolicy
from .schema import validate_snapshot
from .seed import build_catalog, build_fault_script, build_previous_snapshot, build_robots_txt
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
        description="Run the local resilient web collection lab (offline, in-process fixture site).",
    )
    parser.add_argument("--catalog", type=Path, help="JSON array of synthetic product rows for the fixture")
    parser.add_argument("--faults", type=Path, help="JSON array of one-shot fixture faults")
    parser.add_argument("--snapshot", type=Path, help="Previous canonical snapshot used for change detection")
    parser.add_argument("--robots", type=Path, help="robots.txt override for the fixture")
    parser.add_argument("--state-dir", type=Path, help="Directory for snapshot, CSV, and checkpoint files")
    parser.add_argument("--job-id", default="lab-collect")
    parser.add_argument("--user-agent", default=USER_AGENT)
    parser.add_argument("--min-interval-ms", type=_int_in_range(0, 60_000), default=0)
    parser.add_argument("--burst", type=_int_in_range(1, 8), default=1)
    parser.add_argument("--page-size", type=_int_in_range(1, 20), default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--force", action="store_true", help="Ignore ETags and refetch every product page")
    parser.add_argument("--crash-after-products", type=_int_in_range(1, 10_000), default=None)
    parser.add_argument("--print-events", action="store_true")
    parser.add_argument("--log-jsonl", action="store_true", help="Stream structured events to stderr as JSON Lines")
    parser.add_argument("--now-ms", type=_int_in_range(0, 10_000_000_000_000), default=LAB_NOW_MS)
    return parser


def default_examples_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "examples"


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_or_default(path: Optional[Path], default_name: str, builder) -> Any:
    if path is None:
        candidate = default_examples_dir() / default_name
        if candidate.exists():
            path = candidate
        else:
            return builder()
    try:
        payload = _load_json(path)
    except (OSError, ValueError) as exc:
        raise LabInputError(f"could not load {path}: {type(exc).__name__}: {exc}") from exc
    return payload


def _load_catalog(path: Optional[Path]) -> list[dict[str, Any]]:
    payload = _load_or_default(path, "catalog.json", build_catalog)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise LabInputError("catalog must contain a JSON array of objects")
    for index, row in enumerate(payload):
        if not isinstance(row.get("sku"), str) or not isinstance(row.get("slug"), str):
            raise LabInputError(f"catalog[{index}] needs string sku and slug")
    return payload


def _load_faults(path: Optional[Path]) -> list[Fault]:
    payload = _load_or_default(path, "fault_script.json", build_fault_script)
    if not isinstance(payload, list):
        raise LabInputError("faults must contain a JSON array")
    built: list[Fault] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise LabInputError(f"faults[{index}] must be an object")
        try:
            built.append(Fault.from_dict(item))
        except ValueError as exc:
            raise LabInputError(f"faults[{index}]: {exc}") from exc
    return built


def _load_snapshot(path: Optional[Path]) -> dict[str, Any]:
    payload = _load_or_default(path, "previous_snapshot.json", build_previous_snapshot)
    if not isinstance(payload, dict):
        raise LabInputError("snapshot must contain a JSON object")
    try:
        return validate_snapshot(payload)
    except LabError as exc:
        raise LabInputError(f"snapshot: {exc.message}") from exc


def _load_robots(path: Optional[Path]) -> str:
    if path is None:
        candidate = default_examples_dir() / "robots.txt"
        if candidate.exists():
            path = candidate
        else:
            return build_robots_txt()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LabInputError(f"could not load {path}: {exc}") from exc


def run_lab(argv: Optional[list[str]] = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    catalog_rows = _load_catalog(args.catalog)
    faults = _load_faults(args.faults)
    previous_payload = _load_snapshot(args.snapshot)
    robots_txt = _load_robots(args.robots)

    state_dir = args.state_dir
    if state_dir is None and not args.dry_run:
        state_dir = Path(tempfile.mkdtemp(prefix="web-lab-"))

    clock = ManualClock(start_ms=args.now_ms)
    logger = JsonLogger(stream=sys.stderr if args.log_jsonl else None, clock=clock)
    sleeper = RecordingSleeper(clock)
    site = FixtureSite(
        catalog_rows,
        robots_txt=robots_txt,
        faults=faults,
        page_size=args.page_size,
    )
    client, transport, limiter, _inner = build_client(
        site,
        policy=RetryPolicy(),
        logger=logger,
        clock=clock,
        sleeper=sleeper,
        seed=7,
        min_interval_ms=args.min_interval_ms,
        burst=args.burst,
        user_agent=args.user_agent,
        force=args.force,
    )
    dest = CatalogStore(None if args.dry_run else snapshot_path_for(state_dir, args.job_id))
    previous = CatalogStore()
    previous.load_from_snapshot(
        {
            "origin": previous_payload["origin"],
            "collected_at": previous_payload["collected_at"],
            "job_id": previous_payload["job_id"],
            "products": [item.to_dict() for item in previous_payload["products"]],
            "etags": previous_payload["etags"],
        }
    )
    csv_path = None if args.dry_run else csv_path_for(state_dir, args.job_id)
    store = MemoryCheckpointStore() if args.dry_run else FileCheckpointStore(state_dir)
    job = CollectJob(
        client,
        catalog=dest,
        previous=previous,
        store=store,
        logger=logger,
        clock=clock,
        job_id=args.job_id,
        fail_fast=args.fail_fast,
        crash_after_products=args.crash_after_products,
        csv_path=csv_path,
    )
    report = job.run(dry_run=args.dry_run, resume=not args.no_resume)
    payload = report.to_dict()
    if args.dry_run and args.state_dir is None:
        payload["state_dir"] = None
    else:
        payload["state_dir"] = str(state_dir)
    payload["catalog_size"] = len(dest.products)
    payload["sleep_delays_ms"] = list(sleeper.delays)
    payload["site_calls"] = len(site.call_log)
    payload["retries_transport"] = transport.retry_count
    payload["rate_limit_acquires"] = limiter.acquire_count
    if args.print_events:
        payload["events"] = list(logger.events)
    return payload


def _print_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    try:
        payload = run_lab(argv)
    except LabInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SimulatedCrash as exc:
        _print_json({"error": exc.code, "message": exc.message})
        return 3
    except LabError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    _print_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
