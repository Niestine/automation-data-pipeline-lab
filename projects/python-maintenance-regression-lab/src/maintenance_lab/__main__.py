"""CLI entry for the offline catalog maintenance lab."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from .apply import MaintenancePipeline
from .bugs import BUGS
from .catalog import CatalogStore
from .checkpoint import FileCheckpointStore, MemoryCheckpointStore
from .compat import catalog_diff
from .errors import LabError, SchemaError, SimulatedCrash
from .ledger import FeedLedger
from .models import FeedInput, LAB_NOW_MS, Product
from .schema import load_catalog_payload, parse_json_text
from .seed import build_default_feeds, build_products
from .telemetry import JsonLogger, ManualClock


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
        description="Run the local Python maintenance & regression lab (offline, synthetic feeds).",
    )
    parser.add_argument("--catalog", type=Path, help="JSON catalog snapshot to load")
    parser.add_argument(
        "--feed",
        type=Path,
        action="append",
        dest="feeds",
        help="Supplier feed file (repeatable). Default: built-in synthetic weekly dumps",
    )
    parser.add_argument("--state-dir", type=Path, help="Directory for durable JSON state")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without writing; reads --state-dir state if given",
    )
    parser.add_argument("--force", action="store_true", help="Re-apply even if the feed ledger already completed")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--crash-after-rows", type=_int_in_range(1, 10_000), default=None)
    parser.add_argument("--now-ms", type=_int_in_range(0, 4_102_444_800_000), default=LAB_NOW_MS)
    parser.add_argument("--print-events", action="store_true")
    parser.add_argument("--log-jsonl", action="store_true", help="Stream structured events to stderr as JSON lines")
    parser.add_argument("--list-bugs", action="store_true", help="Print the historical bug registry and exit")
    parser.add_argument("--compat-from", type=Path, help="Catalog snapshot A for a compatibility diff")
    parser.add_argument("--compat-to", type=Path, help="Catalog snapshot B for a compatibility diff")
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


def _load_catalog_file(path: Path) -> list[Product]:
    data = _load_json(path)
    try:
        rows = load_catalog_payload(data)
    except SchemaError as exc:
        raise LabInputError(f"catalog is invalid: {exc.message}") from exc
    return [Product.from_validated(row, row["fingerprint"]) for row in rows]


def _load_feeds(paths: Optional[list[Path]]) -> list[FeedInput]:
    if not paths:
        return build_default_feeds()
    feeds: list[FeedInput] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LabInputError(f"could not load {path}: {type(exc).__name__}: {exc}") from exc
        if not data:
            raise LabInputError(f"could not load {path}: empty feed")
        feeds.append(FeedInput(name=path.name, data=data))
    return feeds


def run_lab(argv: Optional[list[str]] = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    if args.list_bugs:
        return {"bugs": [item.to_dict() for item in BUGS]}
    if args.compat_from or args.compat_to:
        if args.compat_from is None or args.compat_to is None:
            raise LabInputError("--compat-from and --compat-to must be used together")
        before_store = CatalogStore(products=_load_catalog_file(args.compat_from))
        after_store = CatalogStore(products=_load_catalog_file(args.compat_to))
        diff = catalog_diff(before_store.snapshot(), after_store.snapshot())
        return {"compat": diff.to_dict()}

    examples = default_examples_dir()
    if args.catalog is not None:
        products = _load_catalog_file(args.catalog)
    else:
        candidate = examples / "catalog.json"
        if candidate.exists():
            products = _load_catalog_file(candidate)
        else:
            products = build_products()
    feeds = _load_feeds(args.feeds)

    dry_run = bool(args.dry_run)
    state_dir: Optional[Path] = args.state_dir
    if state_dir is None and not dry_run:
        state_dir = Path(tempfile.mkdtemp(prefix="maintenance-lab-"))

    clock = ManualClock(args.now_ms)
    logger = JsonLogger(clock=clock, stream=sys.stderr if args.log_jsonl else None)
    # A dry run may read existing state so the preview matches a real rerun,
    # but it never creates or writes files.
    catalog_path = None if state_dir is None else state_dir / "catalog.json"
    if catalog_path is not None and catalog_path.exists():
        catalog = CatalogStore(catalog_path)
    elif dry_run:
        catalog = CatalogStore(products=products)
    else:
        catalog = CatalogStore(catalog_path, products=products)
    ledger = FeedLedger(None if state_dir is None else state_dir / "ledger.json")
    checkpoints: Any
    if state_dir is None or dry_run:
        checkpoints = MemoryCheckpointStore()
    else:
        checkpoints = FileCheckpointStore(state_dir / "checkpoints")

    pipeline = MaintenancePipeline(
        catalog,
        ledger=ledger,
        checkpoints=checkpoints,
        logger=logger,
        clock=clock,
        crash_after_rows=args.crash_after_rows,
        fail_fast=args.fail_fast,
        force=args.force,
    )
    before = catalog.snapshot()
    report = pipeline.ingest(feeds, dry_run=dry_run, resume=not args.no_resume)
    after = catalog.snapshot()
    diff = catalog_diff(before, after)
    payload = report.to_dict()
    payload["state_dir"] = None if state_dir is None else str(state_dir)
    payload["compat"] = diff.to_dict()
    payload["products"] = [item.to_dict() for item in catalog.products()]
    if args.print_events:
        payload["events"] = list(logger.events)
    return payload


def _emit(payload: dict[str, Any]) -> None:
    """Print JSON, falling back to ASCII escapes on consoles such as cp932."""
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        text = json.dumps(payload, indent=2, ensure_ascii=True)
    sys.stdout.write(text + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    try:
        payload = run_lab(argv)
    except LabInputError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    except SimulatedCrash as exc:
        _emit({"error": exc.code, "message": exc.message})
        return 3
    except LabError as exc:
        sys.stderr.write(f"{exc.code}: {exc.message}\n")
        return 1
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
