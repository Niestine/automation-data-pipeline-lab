"""Command line for one scenario, a preemption search, or a journal replay."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from yieldlab.campaign import pct_hits
from yieldlab.engine import Schedule, run, search_first
from yieldlab.errors import BoundExceeded, LabError
from yieldlab.journal import dump_journal, load_journal
from yieldlab.scenarios import build, catalog


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive int")
    return value


def _non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yieldlab")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    run_p = sub.add_parser("run")
    run_p.add_argument("scenario")
    run_p.add_argument("--variant", default="unfixed")
    run_p.add_argument("--mode", choices=("gil", "free"), default="free")
    run_p.add_argument("--seed", type=int, default=0)
    run_p.add_argument("--depth", type=_positive, default=1)
    run_p.add_argument("--switch-interval", type=_positive, default=1)
    run_p.add_argument("--k-budget", type=_positive, default=64)
    run_p.add_argument("--n-max", type=_positive, default=8)
    run_p.add_argument("--dry-run", action="store_true")

    search_p = sub.add_parser("search")
    search_p.add_argument("scenario")
    search_p.add_argument("--variant", default="unfixed")
    search_p.add_argument("--bound", type=_non_negative, default=2)
    search_p.add_argument("--mode", choices=("gil", "free"), default="free")
    search_p.add_argument("--seed", type=int, default=0)
    search_p.add_argument("--save", default="")

    replay_p = sub.add_parser("replay")
    replay_p.add_argument("journal")
    replay_p.add_argument("--variant", default="")
    replay_p.add_argument("--mode", choices=("gil", "free", ""), default="")

    camp = sub.add_parser("campaign")
    camp.add_argument("scenario")
    camp.add_argument("--variant", default="unfixed")
    camp.add_argument("--sample", type=_positive, default=80)
    camp.add_argument("--mode", choices=("gil", "free"), default="free")
    camp.add_argument("--depth", type=_positive, default=1)
    camp.add_argument("--k-budget", type=_positive, default=4)
    camp.add_argument("--n-max", type=_positive, default=4)
    camp.add_argument("--switch-interval", type=_positive, default=1)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    try:
        return _dispatch(args)
    except (ValueError, OSError, BoundExceeded) as exc:
        print(f"yieldlab: error: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "list":
        for name in catalog():
            print(name)
        return 0
    if args.cmd == "run":
        scenario = build(args.scenario, args.variant)
        if args.dry_run:
            steps = sum(len(ops) for ops in scenario.threads.values())
            print(
                json.dumps(
                    {
                        "scenario": scenario.name,
                        "variant": scenario.variant,
                        "threads": len(scenario.threads),
                        "steps": steps,
                        "mode": args.mode,
                        "dry_run": True,
                    }
                )
            )
            return 0
        schedule = Schedule(
            mode=args.mode,
            seed=args.seed,
            depth=args.depth,
            n_max=args.n_max,
            k_budget=args.k_budget,
            switch_interval=args.switch_interval,
        )
        return _emit(scenario, schedule)
    if args.cmd == "search":
        scenario = build(args.scenario, args.variant)
        schedule = Schedule(mode=args.mode, seed=args.seed, depth=1, n_max=8, k_budget=64)
        hit = search_first(scenario, schedule, max_bound=args.bound)
        if hit is None:
            print(json.dumps({"failure": None, "bound": args.bound}))
            return 0
        if args.save:
            dump_journal(Path(args.save), hit.journal)
        print(
            json.dumps(
                {
                    "failure": hit.journal["failure"],
                    "bound": hit.bound,
                    "trace_sha256": hit.journal["trace_sha256"],
                    "steps": len(hit.journal["events"]),
                }
            )
        )
        return 0
    if args.cmd == "replay":
        journal = load_journal(Path(args.journal))
        variant = args.variant or journal["variant"]
        mode = args.mode or journal["mode"]
        scenario = build(journal["scenario"], variant)
        schedule = Schedule(
            mode=mode,
            policy="replay",
            replay_events=journal["events"],
            k_budget=int(journal.get("k_budget", 64)),
            n_max=int(journal.get("n_max", 8)),
            switch_interval=int(journal.get("switch_interval", 1)),
            seed=int(journal.get("seed", 0)),
            depth=int(journal.get("depth", 1)),
        )
        return _emit(scenario, schedule)
    if args.cmd == "campaign":
        scenario = build(args.scenario, args.variant)
        hits = pct_hits(
            scenario,
            sample=args.sample,
            mode=args.mode,
            depth=args.depth,
            k_budget=args.k_budget,
            n_max=args.n_max,
            switch_interval=args.switch_interval,
        )
        floor = 1 / (args.n_max * (args.k_budget ** (args.depth - 1)))
        print(
            json.dumps(
                {
                    "hits": hits,
                    "sample": args.sample,
                    "rate": hits / args.sample,
                    "floor": floor,
                }
            )
        )
        return 0
    raise ValueError(f"unknown command {args.cmd}")


def _emit(scenario, schedule) -> int:
    try:
        trace = run(scenario, schedule)
    except LabError as exc:
        print(
            json.dumps(
                {
                    "failure": type(exc).__name__,
                    "message": str(exc),
                    "trace_sha256": None if exc.journal is None else exc.journal.get("trace_sha256"),
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "failure": None,
                "trace_sha256": trace.sha256,
                "races": len(trace.races),
                "violations": len(trace.violations),
                "steps": len(trace.events),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
