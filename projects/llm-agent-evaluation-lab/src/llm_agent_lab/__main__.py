"""CLI entry for the offline agent lab."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .evaluation import evaluate, load_gold, load_tickets
from .orchestrator import AgentOrchestrator
from .provider import FakeProvider, HeuristicProvider
from .store import RunStore
from .telemetry import JsonLogger, ManualClock, RecordingSleeper
from .tools import RecordStore


class LabInputError(Exception):
    """Raised when example/input files are missing or malformed."""


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local LLM agent evaluation lab (offline, no hosted APIs).",
    )
    parser.add_argument("--tickets", type=Path, help="JSON array of synthetic tickets")
    parser.add_argument("--gold", type=Path, help="JSON array of gold labels")
    parser.add_argument("--records", type=Path, help="JSON object of synthetic records")
    parser.add_argument("--script", type=Path, help="FakeProvider script JSON")
    parser.add_argument("--provider", choices=("heuristic", "fake"), default="heuristic")
    parser.add_argument("--dry-run", action="store_true", help="Do not mutate the record store")
    parser.add_argument("--approve", action="store_true", help="Treat require_approval actions as approved")
    return parser


def default_examples_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "examples"


def run_lab(argv: Optional[list[str]] = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    examples = default_examples_dir()
    tickets_path = args.tickets or (examples / "tickets.json")
    gold_path = args.gold or (examples / "gold_labels.json")
    records_path = args.records or (examples / "records.json")
    script_path = args.script or (examples / "provider_script.json")

    try:
        tickets = load_tickets(_load_json(tickets_path))
        gold = load_gold(_load_json(gold_path))
        records = RecordStore(_load_json(records_path))
        if args.provider == "fake":
            provider: Any = FakeProvider(_load_json(script_path))
        else:
            provider = HeuristicProvider()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # json.JSONDecodeError is a ValueError; malformed rows raise KeyError/ValueError.
        raise LabInputError(f"could not load lab inputs: {type(exc).__name__}: {exc}") from exc

    clock = ManualClock()
    logger = JsonLogger()
    orchestrator = AgentOrchestrator(
        provider,
        records=records,
        store=RunStore(),
        logger=logger,
        clock=clock,
        sleeper=RecordingSleeper(clock),
        seed=7,
    )

    results = [
        orchestrator.run(ticket, dry_run=args.dry_run, approve=args.approve)
        for ticket in tickets
    ]
    report = evaluate(results, gold)
    return {
        "provider": provider.name,
        "dry_run": args.dry_run,
        "approve": args.approve,
        "results": [item.to_dict() for item in results],
        "evaluation": report,
        "mutations": records.mutations,
        "log_events": [item["event"] for item in logger.events],
    }


def main(argv: Optional[list[str]] = None) -> int:
    try:
        payload = run_lab(argv)
    except LabInputError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
