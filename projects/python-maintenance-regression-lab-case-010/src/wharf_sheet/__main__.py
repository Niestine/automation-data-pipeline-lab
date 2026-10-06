"""Command line for the wharf intake-sheet lab. Offline, synthetic files only."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .corpus import case_problems, catalog_problems, load_corpus
from .differential import campaign
from .logsetup import LOG
from .model import ParseFailure, ParseSuccess
from .recognize import hardened_parse, legacy_parse

PROJECT = Path(__file__).resolve().parents[2]


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wharf_sheet")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check")
    check.add_argument("--corpus", type=Path, default=PROJECT / "corpus")
    check.add_argument("--verbose", action="store_true", help="log every expected failure and repair")

    parse = sub.add_parser("parse")
    parse.add_argument("file", type=Path)
    parse.add_argument("--header", choices=("present", "absent"), default="absent")
    parse.add_argument("--profile", choices=("hardened", "legacy"), default="hardened")
    parse.add_argument("--charset", default="utf-8")
    parse.add_argument("--field-limit", type=_positive_int, default=4096)

    diff = sub.add_parser("differential")
    diff.add_argument("--seed", type=int, default=20261006)
    diff.add_argument("--budget", type=_positive_int, default=24)
    diff.add_argument("--field-limit", type=_positive_int, default=8)
    diff.add_argument("--verbose", action="store_true", help="log witness repairs and normalizations")

    args = parser.parse_args(argv)
    # parse is the single-file debugging command, so it always logs. check and
    # differential run many inputs that are expected to fail; they log on request.
    if args.command == "parse" or args.verbose:
        _configure_logging()
    if args.command == "check":
        return _check(args.corpus)
    if args.command == "parse":
        return _parse(args)
    if args.command == "differential":
        return _differential(args)
    return 2


def _check(path: Path) -> int:
    cases = load_corpus(path)
    problems = catalog_problems(cases)
    for case in cases:
        problems.extend(case_problems(case))
    payload = {"cases": len(cases), "problems": len(problems)}
    json.dump(payload, sys.stdout, ensure_ascii=True, sort_keys=True)
    sys.stdout.write("\n")
    if problems:
        for item in problems:
            print(item, file=sys.stderr)
        return 1
    return 0


def _parse(args) -> int:
    try:
        data = args.file.read_bytes()
    except OSError as exc:
        print(f"cannot read input: {exc.strerror or type(exc).__name__}", file=sys.stderr)
        return 2
    fn = hardened_parse if args.profile == "hardened" else legacy_parse
    result = fn(data, header=args.header, charset=args.charset, field_limit=args.field_limit)
    json.dump(_public(result), sys.stdout, ensure_ascii=True, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if isinstance(result, ParseSuccess) else 1


def _differential(args) -> int:
    result = campaign(seed=args.seed, budget=args.budget, field_limit=args.field_limit)
    payload = {
        "agreements": result.agreements,
        "classes": list(result.classes),
        "files_written": result.files_written,
        "unclassified": result.unclassified,
        "witnesses": len(result.witnesses),
    }
    json.dump(payload, sys.stdout, ensure_ascii=True, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result.unclassified == 0 and result.files_written == 0 else 1


def _public(result: ParseSuccess | ParseFailure) -> dict:
    events = [
        {
            "byte_offset": event.byte_offset,
            "decision_id": event.decision_id,
            "field_index": event.field_index,
            "record_index": event.record_index,
        }
        for event in result.events
    ]
    if isinstance(result, ParseSuccess):
        return {
            "bom_stripped": result.bom_stripped,
            "events": events,
            "header": list(result.header) if result.header is not None else None,
            "ok": True,
            "records": [list(row) for row in result.records],
        }
    return {
        "bom_stripped": result.bom_stripped,
        "byte_offset": result.byte_offset,
        "code": result.code,
        "decision_id": result.decision_id,
        "events": events,
        "field_index": result.field_index,
        "ok": False,
        "record_index": result.record_index,
    }


def _configure_logging() -> None:
    if any(not isinstance(handler, logging.NullHandler) for handler in LOG.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


if __name__ == "__main__":
    raise SystemExit(main())
