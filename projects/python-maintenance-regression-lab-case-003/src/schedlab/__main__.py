"""Command line for a campaign, a replay, the random screen, and the PCT budget."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from schedlab.artifact import dump, load, report_path, schedule_of
from schedlab.campaign import (
    SCREEN_SEEDS,
    minimum_runs,
    per_run_lower_bound,
    report_from,
    run_campaign,
    trivial_screen,
)
from schedlab.errors import SchedlabError
from schedlab.native import native_smoke
from schedlab.oracles import is_subject_failure
from schedlab.replay import replay
from schedlab.subjects import get_subject

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schedlab")
    sub = parser.add_subparsers(dest="command", required=True)

    campaign = sub.add_parser("campaign")
    campaign.add_argument("--subject", required=True)
    campaign.add_argument("--revision", required=True, choices=("buggy", "fixed"))
    campaign.add_argument(
        "--policy",
        required=True,
        choices=("pct", "preemption", "delay", "random", "dfs", "fair"),
    )
    campaign.add_argument("--d", type=int, default=1)
    campaign.add_argument("--seeds", default="0")
    campaign.add_argument("--n-max", type=int, default=3)
    campaign.add_argument("--k", type=int, default=None)
    campaign.add_argument("--schedule-cap", type=int, default=500)
    campaign.add_argument("--max-bound", type=int, default=5)
    campaign.add_argument("--pad", type=int, default=0)
    campaign.add_argument("--artifact-dir", type=Path, default=None)

    replay_cmd = sub.add_parser("replay")
    replay_cmd.add_argument("artifact", type=Path)
    replay_cmd.add_argument("--revision", choices=("buggy", "fixed"), default=None)
    replay_cmd.add_argument("--artifact-dir", type=Path, default=None)

    screen = sub.add_parser("screen")
    screen.add_argument("--max-k", type=int, default=32)

    budget = sub.add_parser("budget")
    budget.add_argument("--n", type=int, required=True)
    budget.add_argument("--k", type=int, required=True)
    budget.add_argument("--d", type=int, required=True)
    budget.add_argument("--delta", type=float, default=0.01)

    sub.add_parser("smoke")

    args = parser.parse_args(argv)
    try:
        if args.command == "budget":
            return _budget(args.n, args.k, args.d, args.delta)
        if args.command == "smoke":
            print(json.dumps(native_smoke(), indent=2, sort_keys=True, default=str))
            return 0
        if args.command == "screen":
            return _screen(args.max_k)
        if args.command == "replay":
            return _replay(args.artifact, args.revision, args.artifact_dir)
        if args.command == "campaign":
            return _campaign(args)
    except (SchedlabError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("error: unknown command", file=sys.stderr)
    return 2


def _budget(n_max: int, k: int, depth: int, delta: float) -> int:
    probability = per_run_lower_bound(n_max, k, depth)
    runs = minimum_runs(n_max, k, depth, delta=delta)
    print(f"p={probability}")
    print(f"R={runs}")
    print(f"delta={delta}")
    print(f"bound=1/(n*k**(d-1)) for d={depth} only")
    return 0


def _screen(max_k: int) -> int:
    report = trivial_screen(SCREEN_SEEDS, max_k=max_k)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["headline"] is None:
        return 1
    return 0


def _replay(path: Path, revision: str | None, artifact_dir: Path | None) -> int:
    payload = load(path)
    chosen = revision or payload["revision"]
    subject = get_subject(payload["subject"], chosen, pad=payload.get("pad", 0))
    machine = replay(
        subject,
        schedule_of(payload),
        n_max=payload["n_max"],
        k=payload["k"],
    )
    print(f"terminal={machine.terminal}")
    print(f"schedule={machine.schedule}")
    print(f"cells={machine.cells}")
    if artifact_dir is not None:
        # The replay gets its own artifact with this interpreter's GIL stamp.
        report = report_from(
            machine,
            policy="replay",
            seed=payload["seed"],
            d=payload["d"],
            change_points=payload["change_points"],
            schedules_used=1,
        )
        written = dump(report_path(artifact_dir, report), report)
        print(f"artifact={written}")
    if machine.terminal == "pass":
        return 0
    if is_subject_failure(machine.terminal or ""):
        return 1
    # diverged, step-cap, and bound_exceeded are inconclusive, not a pass.
    return 2


def _campaign(args: argparse.Namespace) -> int:
    directory = args.artifact_dir
    if directory is None:
        directory = PROJECT_ROOT / "artifacts"
    report = run_campaign(
        args.subject,
        args.revision,
        args.policy,
        seeds=_parse_seeds(args.seeds),
        n_max=args.n_max,
        k=args.k,
        d=args.d,
        schedule_cap=args.schedule_cap,
        artifact_dir=directory,
        max_bound=args.max_bound,
        pad=args.pad,
    )
    print(
        f"terminal={report.terminal} schedules_used={report.schedules_used} "
        f"steps={report.steps} coverage={report.coverage}"
    )
    print(f"schedule={report.schedule}")
    if is_subject_failure(report.terminal):
        return 1
    return 0


def _parse_seeds(text: str) -> list[int]:
    seeds: list[int] = []
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            start_text, end_text = piece.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"seed range {piece} is reversed")
            seeds.extend(range(start, end + 1))
        else:
            seeds.append(int(piece))
    if not seeds:
        raise ValueError("seeds are empty")
    return seeds


if __name__ == "__main__":
    raise SystemExit(main())
