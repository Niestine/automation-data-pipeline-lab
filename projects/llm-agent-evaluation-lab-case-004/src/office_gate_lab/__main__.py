"""CLI for the offline office-gate lab."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .errors import LabInputError
from .metrics import evaluate_suite

EXIT_INPUT = 2


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LabInputError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LabInputError(f"malformed JSON: {path}") from exc


TASK_KEYS = ("id", "request", "contract_tools", "golden_outbox", "plan", "finish")


def check_fixtures(world: Any, tasks: Any) -> None:
    """Fail with LabInputError, not a traceback, on a fixture of the wrong shape."""
    if not isinstance(world, dict):
        raise LabInputError("world must be an object")
    for collection, keys in (
        ("documents", ("id", "readers", "title", "body")),
        ("messages", ("id", "readers", "sender", "subject", "body")),
    ):
        records = world.get(collection)
        if not isinstance(records, list):
            raise LabInputError(f"world.{collection} must be a list")
        for record in records:
            if not isinstance(record, dict) or any(key not in record for key in keys):
                raise LabInputError(f"world.{collection} record is missing a field")
    if not isinstance(tasks, list) or not tasks:
        raise LabInputError("tasks must be a non-empty list")
    ids = []
    for task in tasks:
        if not isinstance(task, dict) or any(key not in task for key in TASK_KEYS):
            raise LabInputError("task is missing a field: " + ", ".join(TASK_KEYS))
        golden = task["golden_outbox"]
        if not isinstance(golden, list) or any(
            not isinstance(row, dict) or "document_id" not in row or "recipient" not in row for row in golden
        ):
            raise LabInputError(f"task {task['id']} golden_outbox rows need document_id and recipient")
        if not isinstance(task["contract_tools"], list):
            raise LabInputError(f"task {task['id']} contract_tools must be a list")
        ids.append(task["id"])
    if len(ids) != len(set(ids)):
        raise LabInputError("task ids must be unique")


def _hijack_line(name: str, row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    return (
        f"{name} task={row['task_id']} status={row['status']} "
        f"outbox_count={row['outbox_count']} "
        f"targeted_attack_success={row['targeted_attack_success']}"
    )


def format_report(report: dict[str, Any]) -> str:
    pair = report["meeting_notes_pair"]
    collapse = report["refuse_all"]
    lines = [
        "office gate lab",
        f"tasks={report['task_count']} k={report['k']}",
        f"benign_utility={report['benign_utility']:.3f}",
        f"utility_under_attack={report['utility_under_attack']:.3f}",
        f"targeted_attack_success={report['targeted_attack_success']:.3f}",
        f"policy_violation_rate={report['policy_violation_rate']:.3f}",
        f"incomplete_rate={report['incomplete_rate']:.3f}",
        f"policy_fail_rate={report['policy_fail_rate']:.3f}",
        f"pass^1={report['pass_hat_1']:.3f}",
        f"pass^k={report['pass_hat_k']:.3f}",
        f"pass@k={report['pass_at_k']:.3f}",
        f"reliability_headline={report['reliability_headline']}",
        (
            "meeting_notes_pair "
            f"benign_utility={pair['benign_utility']} "
            f"utility_under_attack={pair['utility_under_attack']} "
            f"targeted_attack_success={pair['targeted_attack_success']}"
        )
        if pair is not None
        else None,
        _hijack_line("dataflow_hijack", report["dataflow_hijack"]),
        _hijack_line("dataflow_hijack_undefended", report["dataflow_hijack_undefended"]),
        (
            "refuse_all "
            f"benign_utility={collapse['benign_utility']:.3f} "
            f"targeted_attack_success={collapse['targeted_attack_success']:.3f} "
            f"label={collapse['label']}"
        ),
    ]
    return "\n".join(line for line in lines if line is not None) + "\n"


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the office-gate evaluation suite.")
    parser.add_argument("--world", type=Path, default=None)
    parser.add_argument("--tasks", type=Path, default=None)
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args(argv)
    root = project_root()
    world_path = args.world or (root / "examples" / "world.json")
    tasks_path = args.tasks or (root / "examples" / "tasks.json")
    out = stdout or sys.stdout
    try:
        if args.trials < 1:
            raise LabInputError("trials must be >= 1")
        world = load_json(world_path)
        payload = load_json(tasks_path)
        tasks = payload.get("tasks") if isinstance(payload, dict) else payload
        check_fixtures(world, tasks)
        report = evaluate_suite(world, tasks, trials=args.trials)
    except LabInputError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INPUT
    out.write(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
