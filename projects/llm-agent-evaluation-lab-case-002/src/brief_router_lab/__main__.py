"""CLI entry for the offline tool-routing lab."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .evaluation import evaluate, load_gold, load_packets
from .orchestrator import BriefRouter
from .provider import FakePlanner, HeuristicPlanner
from .store import RunStore
from .telemetry import JsonLogger, ManualClock, RecordingSleeper
from .workspace import BriefWorkspace, ToolFaults


class LabInputError(Exception):
    """Raised when example/input files are missing or malformed."""


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _require_script_map(payload: Any, label: str) -> dict[str, list[dict[str, Any]]]:
    """FakePlanner scripts and tool-fault scripts are {packet_id: [object, ...]}."""
    if not isinstance(payload, dict):
        raise LabInputError(f"{label} file must be a JSON object")
    for key, steps in payload.items():
        if not isinstance(steps, list) or not all(isinstance(item, dict) for item in steps):
            raise LabInputError(f"{label} entry {key!r} must be a list of objects")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local LLM tool-routing evaluation lab (offline, no hosted APIs).",
    )
    parser.add_argument("--packets", type=Path, help="JSON array of synthetic work packets")
    parser.add_argument("--gold", type=Path, help="JSON array of gold routing labels")
    parser.add_argument("--workspace", type=Path, help="JSON object of synthetic assets/notes/slots")
    parser.add_argument("--script", type=Path, help="FakePlanner script JSON")
    parser.add_argument("--faults", type=Path, help="Per-packet tool fault script JSON")
    parser.add_argument("--provider", choices=("heuristic", "fake"), default="heuristic")
    parser.add_argument("--dry-run", action="store_true", help="Execute reads; skip write mutations")
    parser.add_argument("--approve", action="store_true", help="Release require_approval plans")
    return parser


def default_examples_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "examples"


def _workspace_from_payload(payload: dict[str, Any], faults: ToolFaults) -> BriefWorkspace:
    return BriefWorkspace(
        assets=payload.get("assets"),
        notes=payload.get("notes"),
        slots=payload.get("slots"),
        faults=faults,
    )


def run_lab(argv: Optional[list[str]] = None) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    examples = default_examples_dir()
    packets_path = args.packets or (examples / "packets.json")
    gold_path = args.gold or (examples / "gold_labels.json")
    workspace_path = args.workspace or (examples / "workspace.json")
    script_path = args.script or (examples / "planner_script.json")
    faults_path = args.faults or (examples / "tool_faults.json")

    try:
        packets = load_packets(_load_json(packets_path))
        gold = load_gold(_load_json(gold_path))
        workspace_payload = _load_json(workspace_path)
        if not isinstance(workspace_payload, dict):
            raise LabInputError("workspace file must be a JSON object")
        faults_payload: dict[str, Any] = {}
        if args.faults is not None or faults_path.exists():
            faults_payload = _require_script_map(_load_json(faults_path), "faults")
        workspace = _workspace_from_payload(workspace_payload, ToolFaults(faults_payload))
        if args.provider == "fake":
            provider: Any = FakePlanner(_require_script_map(_load_json(script_path), "script"))
        else:
            provider = HeuristicPlanner()
    except LabInputError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise LabInputError(f"could not load lab inputs: {type(exc).__name__}: {exc}") from exc

    clock = ManualClock()
    logger = JsonLogger()
    router = BriefRouter(
        provider,
        workspace=workspace,
        store=RunStore(),
        logger=logger,
        clock=clock,
        sleeper=RecordingSleeper(clock),
        seed=11,
    )

    results = [
        router.run(packet, dry_run=args.dry_run, approve=args.approve)
        for packet in packets
    ]
    report = evaluate(results, gold)
    return {
        "provider": provider.name,
        "dry_run": args.dry_run,
        "approve": args.approve,
        "results": [item.to_dict() for item in results],
        "evaluation": report,
        "mutations": workspace.mutations,
        "publish_queue": list(workspace.publish_queue),
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
