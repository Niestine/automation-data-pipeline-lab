"""CLI entry for the offline API integration reliability lab."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from .checkpoint import FileCheckpointStore
from .client import build_client
from .errors import LabError, SimulatedCrash
from .ledger import Ledger
from .mock_service import Fault, MockFulfillmentApi
from .models import LAB_TOKEN, LAB_WEBHOOK_SECRET
from .retry import RetryPolicy
from .seed import build_catalog, build_fault_script, build_webhook_events
from .sync import SyncJob
from .telemetry import JsonLogger, ManualClock, RecordingSleeper
from .webhooks import WebhookReceiver, build_delivery


class LabInputError(Exception):
    """Raised when example/input files are missing or malformed."""


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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
        description="Run the local API integration reliability lab (offline, in-process mock).",
    )
    parser.add_argument("--catalog", type=Path, help="JSON array of synthetic orders")
    parser.add_argument("--faults", type=Path, help="JSON array of one-shot source faults")
    parser.add_argument("--webhooks", type=Path, help="JSON array of synthetic webhook events")
    parser.add_argument("--checkpoint-dir", type=Path, help="Directory for durable JSON checkpoints")
    parser.add_argument("--sync-id", default="lab-sync")
    parser.add_argument("--page-limit", type=_int_in_range(1, 50), default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--crash-after-pages", type=_int_in_range(1, 10_000), default=None)
    parser.add_argument(
        "--crash-at",
        choices=("post_upsert", "post_ack", "post_checkpoint"),
        default="post_upsert",
    )
    parser.add_argument("--print-events", action="store_true")
    return parser


def default_examples_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "examples"


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
    if not isinstance(payload, list):
        raise LabInputError(f"{path} must contain a JSON array")
    if not all(isinstance(item, dict) for item in payload):
        raise LabInputError(f"{path} must contain only JSON objects")
    return payload


def _check_catalog(catalog: list[dict[str, Any]]) -> None:
    # Rows may deliberately fail the order schema (poison rows), but the mock
    # still needs a string id and updated_at to key and sort them.
    for index, row in enumerate(catalog):
        if not isinstance(row.get("id"), str) or not isinstance(row.get("updated_at"), str):
            raise LabInputError(f"catalog[{index}] needs string id and updated_at")


def _build_faults(faults: list[dict[str, Any]]) -> list[Fault]:
    built: list[Fault] = []
    for index, item in enumerate(faults):
        try:
            built.append(Fault.from_dict(item))
        except ValueError as exc:
            raise LabInputError(f"faults[{index}]: {exc}") from exc
    return built


def run_lab(argv: Optional[list[str]] = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    catalog = _load_or_default(args.catalog, "catalog.json", build_catalog)
    faults = _load_or_default(args.faults, "fault_script.json", build_fault_script)
    events = _load_or_default(args.webhooks, "webhook_events.json", build_webhook_events)
    _check_catalog(catalog)
    faults = _build_faults(faults)

    checkpoint_dir = args.checkpoint_dir
    if checkpoint_dir is None:
        checkpoint_dir = Path(tempfile.mkdtemp(prefix="api-lab-"))

    clock = ManualClock()
    logger = JsonLogger()
    sleeper = RecordingSleeper(clock)
    service = MockFulfillmentApi(catalog, token=LAB_TOKEN, faults=faults)
    client, transport = build_client(
        service,
        token=LAB_TOKEN,
        policy=RetryPolicy(),
        logger=logger,
        clock=clock,
        sleeper=sleeper,
        seed=7,
        default_limit=args.page_limit,
    )
    ledger = Ledger(checkpoint_dir / f"{args.sync_id}.ledger.json")
    receiver = WebhookReceiver(
        ledger, secret=LAB_WEBHOOK_SECRET, clock=clock, logger=logger
    )
    timestamp = str(clock.now_s())
    deliveries = [
        build_delivery(event, secret=LAB_WEBHOOK_SECRET, timestamp=timestamp)
        for event in events
    ]
    job = SyncJob(
        client,
        ledger=ledger,
        store=FileCheckpointStore(checkpoint_dir),
        receiver=receiver,
        logger=logger,
        clock=clock,
        sync_id=args.sync_id,
        fail_fast=args.fail_fast,
        crash_after_pages=args.crash_after_pages,
        crash_at=args.crash_at,
        page_limit=args.page_limit,
    )
    report = job.run(
        deliveries=deliveries,
        dry_run=args.dry_run,
        resume=not args.no_resume,
    )
    payload = report.to_dict()
    payload["checkpoint_dir"] = str(checkpoint_dir)
    payload["ledger_size"] = len(ledger.orders)
    payload["ack_state_size"] = len(service.ack_state)
    payload["sleep_delays_ms"] = list(sleeper.delays)
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
        # Auth, HTTP, schema, or checkpoint failure: report it without a traceback.
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
