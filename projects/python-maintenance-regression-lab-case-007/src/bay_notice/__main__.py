"""CLI for the bay-hold desk. Prints ASCII JSON and does not open a socket."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .client import BUILDS, Desk
from .errors import BayError
from .inject import Gateway, job_from_dict, load_json, step_from_dict
from .log import LOGGER, JsonFieldFormatter
from .policy import RetryPolicy, tcp_6298_policy
from .timer import RtoEstimator
from .trace import compare


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bay-notice")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="post one synthetic notice through a scripted gate")
    run.add_argument("schedule")
    run.add_argument("--build", default="repaired", choices=sorted(BUILDS))
    run.add_argument(
        "--log",
        action="store_true",
        help="write one JSON line per attempt record to stderr",
    )

    sub.add_parser("timer", help="print the no-sample RTO table for the TCP-shaped preset")

    trace = sub.add_parser("trace", help="score the frozen correlated trace")
    trace.add_argument("path")

    args = parser.parse_args(argv)
    previous_level = LOGGER.level
    handler = _attach_stderr_log() if getattr(args, "log", False) else None
    try:
        if args.command == "run":
            payload = _run(Path(args.schedule), args.build)
        elif args.command == "timer":
            payload = _timer()
        else:
            payload = _trace(Path(args.path))
    except (BayError, OSError, KeyError, TypeError, ValueError) as exc:
        # A gate error, a rejected notice, or a malformed fixture. No traceback.
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    finally:
        if handler is not None:
            LOGGER.removeHandler(handler)
            LOGGER.setLevel(previous_level)
    print(json.dumps(payload, sort_keys=True))
    return 0


def _attach_stderr_log() -> logging.Handler:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFieldFormatter())
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    return handler


def _run(path: Path, build: str) -> dict:
    raw = load_json(path)
    job = job_from_dict(raw["job"])
    policy = RetryPolicy(
        max_retries=int(raw.get("max_retries", 3)),
        floor_s=float(raw.get("floor_s", 0.25)),
        ceiling_s=float(raw.get("ceiling_s", 2.0)),
        granularity_s=float(raw.get("granularity_s", 0.001)),
    )
    steps = [step_from_dict(item) for item in raw.get("steps", [])]
    desk = Desk(
        policy=policy,
        gateway=Gateway(steps),
        timer=RtoEstimator.from_policy(policy),
        defects=BUILDS[build],
    )
    result = desk.run(job)
    return {
        "body": result.body,
        "build": build,
        "commits": result.commits,
        "decisions": result.decisions,
        "delays_s": desk.clock.waits,
        "ok": result.ok,
        "operation_id": result.operation_id,
        "transmissions": result.transmissions,
    }


def _timer() -> dict:
    estimator = RtoEstimator.from_policy(tcp_6298_policy())
    armed: list[float] = []
    for _ in range(8):
        armed.append(estimator.rto)
        estimator.on_timeout()
    return {
        "armed_s": armed,
        "ceiling_s": estimator.ceiling_s,
        "floor_s": estimator.floor_s,
        "profile": "tcp_6298",
    }


def _trace(path: Path) -> dict:
    raw = load_json(path)
    scored = compare(raw["jobs"], raw["draws"], float(raw["base_load"]))
    return {
        name: {"calls": item["calls"], "raf": item["raf"], "successes": item["successes"]}
        for name, item in scored.items()
    }


if __name__ == "__main__":
    raise SystemExit(main())
