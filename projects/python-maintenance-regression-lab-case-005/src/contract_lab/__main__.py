"""Offline commands: metrics, classify, call."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from contract_lab.client import ContractClient
from contract_lab.compliance import cited_figures, review_history
from contract_lab.errors import ContractError
from contract_lab.http_lifecycle import parse_instant
from contract_lab.logsetup import get_logger
from contract_lab.rules import RULES


def _configure_logging() -> None:
    logger = get_logger()
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        if getattr(handler, "contract_lab_cli", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.contract_lab_cli = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _metrics() -> dict:
    payload = cited_figures()
    payload["seeded_rule_count"] = len(RULES)
    payload["api_count_sum_is_not_studied_population"] = (
        payload["best_case_consistent_apis"] + payload["leaking_apis"] + payload["never_breaking_apis"]
        != payload["studied_apis"]
    )
    return payload


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(list(sys.argv[1:] if argv is None else argv))
    except (OSError, ValueError, KeyError) as exc:
        # Unreadable fixture, bad JSON, or a missing fixture key.
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except ContractError as exc:
        # A checked maintenance failure: schema rejection, illegal header, removal.
        print(f"contract failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _run(args: list[str]) -> int:
    command = args[0] if args else "metrics"
    if command == "metrics":
        _print(_metrics())
        return 0
    if command == "classify":
        if len(args) != 2:
            print("usage: classify HISTORY.json", file=sys.stderr)
            return 2
        payload = _load(args[1])
        _print(review_history(payload["documents"], payload["versions"]))
        return 0
    if command == "call":
        if len(args) != 2:
            print("usage: call FIXTURE.json", file=sys.stderr)
            return 2
        _configure_logging()
        payload = _load(args[1])
        now = parse_instant(payload["now"]) if payload.get("now") else datetime(2024, 6, 1, tzinfo=timezone.utc)
        client = ContractClient(reader=payload.get("reader", "tolerant"), now=now)
        history = payload.get("history") or [payload["document"]]
        client.load(payload["document"], history)
        result = client.call(payload["method"], payload["path"], payload["response"])
        _print(
            {
                "business": result.business,
                "events": [event.as_dict() for event in result.events],
                "phase": result.phase,
                "stability_assured": result.stability_assured,
                "status": result.status,
                "readers": {
                    name: {"valid": item["valid"]}
                    for name, item in result.reader_results.items()
                },
            }
        )
        return 0
    print("usage: metrics | classify HISTORY.json | call FIXTURE.json", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
