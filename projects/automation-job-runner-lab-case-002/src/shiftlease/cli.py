"""CLI for the shift-slip lease queue. State stays in the local SQLite file."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from pathlib import Path

from shiftlease.config import Config
from shiftlease.contract import DRAFT_NOTICE, FINGERPRINT_ID
from shiftlease.effect import CsvEffect
from shiftlease.errors import ShiftError
from shiftlease.logjson import JsonLogger
from shiftlease.payload import validate_payload
from shiftlease.store import QueueStore
from shiftlease.worker import Worker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shiftlease",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Local multi-process shift-slip exporter.\n"
            + DRAFT_NOTICE
            + f"\nFingerprint algorithm: {FINGERPRINT_ID}. "
            "A key is retained for retention_seconds after the job reaches succeeded or dead. "
            "Deadlines use the database clock. WAL is single-host."
        ),
    )
    parser.add_argument("--db", required=True, help="SQLite database path. Keep the -wal and -shm files with it.")
    parser.add_argument("--outbox", default="", help="Directory for CSV effects. Required for worker runs.")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="Insert one job, or replay the stored result for the same key.")
    submit.add_argument("--key", default="", help="Idempotency key. A UUID is recommended.")
    submit.add_argument("--payload", default="", help="JSON object with desk and rows.")
    submit.add_argument("--payload-file", default="", help="Path to a JSON payload object.")
    submit.add_argument("--max-attempts", type=int, default=5)

    slot = sub.add_parser("slot", help="Insert sched:{schedule_id}:{slot_start} once.")
    slot.add_argument("--schedule-id", required=True)
    slot.add_argument("--slot-start", required=True, help="UTC slot YYYY-MM-DDTHH:MM:SSZ")
    slot.add_argument("--payload", default="")
    slot.add_argument("--payload-file", default="")
    slot.add_argument("--max-attempts", type=int, default=5)

    worker = sub.add_parser("worker", help="Claim due jobs and write one CSV per idempotency key.")
    worker.add_argument("--owner", default="")
    worker.add_argument("--max-jobs", type=int, default=0, help="Stop after this many claims. 0 drains the queue.")
    worker.add_argument("--seed", type=int, default=20261005)
    worker.add_argument("--dry-run", action="store_true", help="Print the next CSV. Take no lease and write no file.")
    worker.add_argument("--resume", action="store_true", help="Finish leases this owner still holds, then claim.")

    sub.add_parser("status", help="Short read-only counts. Safe beside the single writer.")
    sub.add_parser("sweep", help="Dead-letter expired leases that have used their attempt budget.")
    purge = sub.add_parser("purge", help="Delete terminal rows older than the retention window.")
    purge.add_argument("--yes", action="store_true", help="Required. A purged key can be submitted again.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except ShiftError as exc:
        _emit({"ok": False, "error": exc.code, "http_class": exc.http_class, "message": str(exc)})
        return 2 if exc.http_class in {400, 422} else 1


def _run(args: argparse.Namespace) -> int:
    config = Config()
    store = QueueStore.open(args.db, config)
    try:
        if args.command == "status":
            _emit({"ok": True, **store.status()})
            return 0
        if args.command == "sweep":
            ids = store.sweep_dead()
            _emit({"ok": True, "dead_ids": ids})
            return 0
        if args.command == "purge":
            if not args.yes:
                _emit({"ok": False, "error": "purge_requires_yes", "http_class": 400, "message": "pass --yes"})
                return 2
            removed = store.purge_expired()
            _emit({"ok": True, "purged": removed, "retention_seconds": config.retention_seconds})
            return 0
        if args.command == "submit":
            result = store.submit(_key_or_none(args.key), _load_payload(args), max_attempts=args.max_attempts)
            return _emit_submit(result)
        if args.command == "slot":
            result = store.enqueue_slot(
                args.schedule_id,
                args.slot_start,
                _load_payload(args),
                max_attempts=args.max_attempts,
            )
            return _emit_submit(result)
        if args.command == "worker":
            return _worker(args, store, config)
        _emit({"ok": False, "error": "unknown_command", "message": args.command})
        return 2
    finally:
        store.close()


def _worker(args: argparse.Namespace, store: QueueStore, config: Config) -> int:
    owner = args.owner or f"w-{uuid.uuid4().hex[:12]}"
    logger = JsonLogger(sys.stderr)
    worker = Worker(
        store,
        CsvEffect(Path(args.outbox or ".")),
        owner,
        config,
        random.Random(args.seed),
        _real_sleep,
        logger,
    )
    if args.dry_run:
        preview = worker.preview()
        _emit({"ok": True, "dry_run": True, "owner": owner, "preview": preview})
        return 0
    if not args.outbox:
        _emit({"ok": False, "error": "outbox_required", "http_class": 400, "message": "pass --outbox"})
        return 2
    if args.resume:
        worker.resume_held()
    if args.max_jobs > 0:
        worker.start_heartbeat()
        done = 0
        outcome = "empty"
        try:
            while done < args.max_jobs:
                outcome = worker.run_once()
                if outcome == "job":
                    done += 1
                    continue
                if outcome in {"quarantine", "jeopardy"}:
                    break
                if outcome == "empty":
                    break
        finally:
            worker.stop_heartbeat()
        _emit({"ok": outcome not in {"quarantine", "jeopardy"}, "owner": owner, "outcome": outcome, "jobs": done})
        if outcome == "quarantine":
            return 3
        return 1 if outcome == "jeopardy" else 0
    outcome = worker.run_until_idle()
    report = store.status()
    _emit({"ok": outcome == "drained", "owner": owner, "outcome": outcome, "status": report})
    if outcome == "quarantine":
        return 3
    return 0 if outcome == "drained" else 1


def _emit_submit(result) -> int:
    _emit(
        {
            "ok": result.http_class != 409,
            "outcome": result.outcome,
            "http_class": result.http_class,
            "job_id": result.job_id,
            "status": result.status,
            "attempts": result.attempts,
            "idempotency_key": result.idempotency_key,
            "result_json": result.result_json,
            "last_error": result.last_error,
            "fingerprint": FINGERPRINT_ID,
        }
    )
    if result.http_class == 409:
        return 4
    return 0


def _load_payload(args: argparse.Namespace) -> object:
    if args.payload_file:
        raw = Path(args.payload_file).read_text(encoding="utf-8")
    elif args.payload:
        raw = args.payload
    else:
        raise ShiftError("missing_payload", "pass --payload or --payload-file", 400)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ShiftError("invalid_json", "payload is not JSON", 400) from exc
    return validate_payload(parsed)


def _key_or_none(key: str) -> str | None:
    return key if key else None


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _real_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
