"""CLI for the offline plot-allotment export."""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from pathlib import Path

from .clock import ManualClock
from .client import ExportClient
from .errors import CheckpointIOError, LabError, SchemaError
from .journal import Journal
from .schema import require_parent, resource_body
from .service import Service
from .store import Store
from .worker import ExportWorker


def _default_examples() -> Path:
    return Path(__file__).resolve().parents[2] / "examples"


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(f"cannot read {path.name}") from exc
    if not isinstance(value, dict):
        raise SchemaError(f"{path.name} must be an object")
    return value


def _load_source(store: Store, document: dict) -> str:
    parent = document.get("parent")
    resources = document.get("resources")
    if not isinstance(parent, str) or not isinstance(resources, list):
        raise SchemaError("allotments file needs parent and resources")
    parent = require_parent(parent)
    # Validate every row before the first insert so a bad file seeds nothing.
    rows = [resource_body(item, allow_request_id=False) for item in resources]
    if store.live_ids(parent):
        return parent
    for fields in rows:
        store.insert_source(parent, fields)
    return parent


def _build(db_path: str, seed: int):
    clock = ManualClock()
    rng = random.Random(seed)
    journal = Journal()
    store = Store(db_path, journal)
    service = Service(store, clock, journal, rng)
    client = ExportClient(
        service,
        "desk-main",
        sleeper=clock.advance,
        rng=rng,
        journal=journal,
    )
    worker = ExportWorker(service, store, client, journal, clock, owner="worker-main")
    return store, service, worker, journal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plot_allotment")
    parser.add_argument("--state", default="", help="directory for the sqlite file")
    parser.add_argument("--allotments", default="", help="synthetic source JSON")
    parser.add_argument("--faults", default="", help="page size and filter JSON")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--crash-before-checkpoint", action="store_true")
    parser.add_argument("--seed", type=int, default=3)
    args = parser.parse_args(argv)
    examples = _default_examples()
    allotment_path = Path(args.allotments) if args.allotments else examples / "allotments.json"
    fault_path = Path(args.faults) if args.faults else examples / "fault_script.json"
    try:
        allotments = _load_json(allotment_path)
        faults = _load_json(fault_path)
        page_size = int(faults.get("page_size", 2))
        filter_raw = str(faults.get("filter", "note=open"))
        order = str(faults.get("order", "sort_key"))
        if page_size < 1:
            raise SchemaError("page_size must be >= 1")
    except (SchemaError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.crash_before_checkpoint and not args.state:
        print("error: --crash-before-checkpoint requires --state", file=sys.stderr)
        return 2

    cleanup = None
    if args.state:
        state = Path(args.state)
        try:
            state.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        db_path = str(state / "allotments.sqlite")
    else:
        cleanup = tempfile.TemporaryDirectory(prefix="plot-allotment-")
        db_path = str(Path(cleanup.name) / "allotments.sqlite")

    store, _service, worker, journal = _build(db_path, args.seed)
    try:
        parent = _load_source(store, allotments)
        scope = "garden-export"
        if args.dry_run:
            worker.dry_run = True
            report = worker.run(scope, parent, filter_raw, order, page_size)
            _print_report(parent, store, report, journal, crashed=False)
            return 0
        existing = store.checkpoint(scope)
        if args.crash_before_checkpoint:
            store.fail_next_checkpoint = True
            worker.run(scope, parent, filter_raw, order, page_size, mode="split")
            print("error: checkpoint injection did not fire", file=sys.stderr)
            return 1
        if existing is None:
            store.fail_next_checkpoint = True
            try:
                worker.run(scope, parent, filter_raw, order, page_size, mode="split")
            except CheckpointIOError:
                report = worker.run(scope, parent, filter_raw, order, page_size, mode="split")
                _print_report(parent, store, report, journal, crashed=True)
                return 0
            print("error: checkpoint injection did not fire", file=sys.stderr)
            return 1
        report = worker.run(scope, parent, filter_raw, order, page_size, mode="split")
        _print_report(parent, store, report, journal, crashed=False)
        return 0
    except CheckpointIOError:
        checkpoint = store.checkpoint("garden-export")
        ahead = bool(checkpoint and checkpoint["apply_ahead"])
        print(
            json.dumps(
                {
                    "apply_ahead": ahead,
                    "crashed": True,
                    "ledger_rows": store.ledger_count(parent),
                    "state": db_path,
                },
                sort_keys=True,
            )
        )
        return 3
    except SchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except LabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
        if cleanup is not None:
            cleanup.cleanup()


def _print_report(parent, store, report, journal, crashed: bool) -> None:
    ids = store.ledger_ids(parent)
    # The ledger primary key alone cannot show a duplicate. A replay that opened
    # a new snapshot would derive new keys and leave two completed records.
    upserts = store.completed_upserts_by_resource(parent)
    payload = {
        "crashed_then_resumed": crashed,
        "dry_run": report.dry_run,
        "duplicate_executions": sum(1 for count in upserts.values() if count > 1),
        "faults": journal.faults(),
        "ledger_rows": len(ids),
        "pages_this_run": report.pages,
        "parent": parent,
        "replayed": report.replayed,
        "seen_ids": report.seen_ids,
        "snapshot_id": report.snapshot_id,
    }
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
