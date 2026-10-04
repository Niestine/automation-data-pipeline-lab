"""CLI for the offline SLIP maintenance lab."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path

from slip_lab.budget import apply_cap
from slip_lab.checkrun import run_check
from slip_lab.differential import classify_inprocess, classify_isolated
from slip_lab.errors import LabError
from slip_lab.ledger import sha256
from slip_lab.logging_setup import configure, log_case, teardown
from slip_lab.model import PRIORITY
from slip_lab.paths import CORPUS
from slip_lab.shrink import make_interesting, shrink
from slip_lab.splice import depth_rate, generate_batch
from slip_lab.stock import QUOTE_ROW, SPECS, defects


def check_exit(payload: dict) -> int:
    """0 when the stock metrics are green, else 1."""

    if (
        payload.get("gate_problems")
        or payload.get("metamorphic_violations")
        or payload.get("outcome_coverage") != 1.0
        or payload.get("disposition_coverage") != 1.0
    ):
        return 1
    return 0


def _emit(payload: dict, stdout, code: int) -> int:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if stdout is None:
        sys.stdout.write(text)
    else:
        stdout.write(text)
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slip_lab")
    parser.add_argument("--list-defects", action="store_true")
    parser.add_argument("--shrink-demo", action="store_true")
    parser.add_argument("--splice", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--commit-dir", type=Path)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--cap", type=int, default=2)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--run-id", default="offline")
    parser.add_argument("--blob", type=Path)
    parser.add_argument("--isolate-one", type=Path)
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser


def _shrink_demo() -> dict:
    parent = b"ZZ|9\n" + QUOTE_ROW + b"PAD|0\n"
    interesting = make_interesting(parent, classify_inprocess)
    reduced = shrink(parent, interesting)
    return {
        "dry_run": True,
        "parent_length": len(parent),
        "reduced_hex": reduced.hex(),
        "reduced_length": len(reduced),
        "shrink_ratio": len(reduced) / len(parent),
    }


def _validate(args: argparse.Namespace) -> None:
    """Refuse a bad request before any parser runs."""

    if args.cap < 0:
        raise LabError("--cap must be 0 or more")
    if args.timeout <= 0:
        raise LabError("--timeout must be greater than 0")
    if args.commit:
        if args.commit_dir is None:
            raise LabError("commit requires --commit-dir")
        target = args.commit_dir.resolve()
        corpus = CORPUS.resolve()
        if target == corpus or corpus in target.parents:
            raise LabError("--commit-dir must be outside the corpus")


def _splice(seed: int, cap: int, commit: bool, commit_dir: Path | None) -> dict:
    sources = [(spec["id"], spec["blob"]) for spec in SPECS]
    batch = generate_batch(sources, seed)
    failures = []
    for item in batch:
        outcome = classify_inprocess(item.blob)
        if outcome.kind != "AGREE":
            failures.append(item.blob)
    capped = apply_cap(failures, cap, lambda blob: make_interesting(blob, classify_inprocess))
    written = []
    if commit and commit_dir is not None:
        commit_dir.mkdir(parents=True, exist_ok=True)
        for index, blob in enumerate(capped["committed"]):
            path = commit_dir / f"splice-{seed}-{index}.slip"
            path.write_bytes(blob)
            written.append(str(path.name))
    return {
        "cap": cap,
        "committed": len(capped["committed"]),
        "dry_run": not commit,
        "median_shrink_ratio": capped["median_shrink_ratio"],
        "malformed_probes": sum(1 for item in batch if item.slot == "malformed_probe"),
        "overflow": capped["overflow"],
        "splice_depth_rate": depth_rate(batch),
        "written": written,
    }


def _one(path: Path, isolated: bool, timeout: float) -> dict:
    if not path.is_file():
        raise LabError(f"blob not found: {path}")
    blob = path.read_bytes()
    if isolated:
        outcome = classify_isolated(blob, timeout=timeout)
    else:
        outcome = classify_inprocess(blob)
    log_case(
        logging.getLogger("slip_lab.one"),
        case_id=path.name,
        outcome=outcome.kind,
        side=outcome.side,
        disposition=None,
        byte_length=len(blob),
        digest=sha256(blob),
        parent_ids=[],
        truncated=outcome.truncated,
        exc_type=outcome.legacy.exc_type or outcome.hardened.exc_type,
    )
    return {
        "dry_run": True,
        "file": path.name,
        "outcome": outcome.kind,
        "side": outcome.side,
        "timeout_s": outcome.timeout_s,
        "truncated": outcome.truncated,
    }


def main(argv: list[str] | None = None, stdout=None) -> int:
    args = _parser().parse_args(argv)
    if args.list_defects:
        payload = {
            "defects": [
                {
                    "disposition": spec.get("disposition"),
                    "file": spec["rel"],
                    "id": spec["id"],
                }
                for spec in defects()
            ]
        }
        return _emit(payload, stdout, 0)
    log_dir = args.log_dir
    temporary = None
    if log_dir is None:
        temporary = tempfile.mkdtemp(prefix="slip-log-")
        log_dir = Path(temporary)
    try:
        log_path = configure(log_dir, args.run_id)
        _validate(args)
        if args.shrink_demo:
            payload = _shrink_demo()
        elif args.splice:
            payload = _splice(args.seed, args.cap, args.commit, args.commit_dir)
        elif args.isolate_one is not None:
            payload = _one(args.isolate_one, True, args.timeout)
        elif args.blob is not None:
            payload = _one(args.blob, False, args.timeout)
        else:
            payload = run_check()
            payload["log_file"] = str(log_path)
            payload["priority"] = [name for name, _rank in sorted(PRIORITY.items(), key=lambda item: item[1])]
            return _emit(payload, stdout, check_exit(payload))
        payload["log_file"] = str(log_path)
        return _emit(payload, stdout, 0)
    except LabError as exc:
        logging.getLogger("slip_lab.config").error("config_error=%s", type(exc).__name__)
        return _emit({"error": str(exc)}, stdout, 2)
    finally:
        teardown()


if __name__ == "__main__":
    raise SystemExit(main())
