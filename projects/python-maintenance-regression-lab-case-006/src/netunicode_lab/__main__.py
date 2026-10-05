"""CLI for the partner-note interchange lab. Output is ASCII JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

from .diagnose import diagnose_mojibake
from .emit import assert_interchange_bytes, emit_interchange_csv
from .errors import LabError
from .ingest import read_foreign_text
from .logsetup import LOGGER


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="netunicode-lab")
    parser.add_argument(
        "--log-signatures",
        action="store_true",
        help="also print the failure signature log line to stderr",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    ingest = subcommands.add_parser("ingest", help="read foreign UTF-8 notes")
    ingest.add_argument("path")

    emit = subcommands.add_parser("emit", help="write a Net-Unicode CSV")
    emit.add_argument("path")
    emit.add_argument("--rows", required=True, help="JSON file of string rows")

    check = subcommands.add_parser("check", help="check interchange bytes")
    check.add_argument("path")

    diagnose = subcommands.add_parser("diagnose", help="count mojibake-shaped pairs")
    diagnose.add_argument("text")

    args = parser.parse_args(argv)
    handler = None
    if args.log_signatures:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        LOGGER.addHandler(handler)
    try:
        if args.command == "ingest":
            payload = _ingest(Path(args.path))
        elif args.command == "emit":
            payload = _emit(Path(args.path), Path(args.rows))
        elif args.command == "check":
            payload = _check(Path(args.path))
        else:
            payload = {"badness": diagnose_mojibake(args.text)}
    except (LabError, OSError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=True),
            file=sys.stderr,
        )
        return 1
    finally:
        if handler is not None:
            LOGGER.removeHandler(handler)
    print(json.dumps(payload, ensure_ascii=True))
    return 0


def _ingest(path: Path) -> dict:
    data = path.read_bytes()
    text = read_foreign_text(path)
    return {
        "logical_text": text,
        "octets": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _emit(path: Path, rows_path: Path) -> dict:
    rows = json.loads(rows_path.read_text(encoding="utf-8"))
    emit_interchange_csv(path, rows)
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "octets": len(data)}


def _check(path: Path) -> dict:
    data = path.read_bytes()
    assert_interchange_bytes(data)
    return {"sha256": hashlib.sha256(data).hexdigest(), "octets": len(data)}


if __name__ == "__main__":
    raise SystemExit(main())
