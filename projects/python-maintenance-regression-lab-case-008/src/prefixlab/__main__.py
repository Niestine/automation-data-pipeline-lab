"""Command line. ``metrics`` prints oracle counts as JSON; ``demo`` prints a one-line summary."""

from __future__ import annotations

import json
import sys

from prefixlab.audit import collect, load_json, publish_audit, recovery_audit
from prefixlab.logging_setup import configure


def main(argv: list[str] | None = None) -> int:
    configure(stream=sys.stderr)
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "metrics"
    if command == "metrics":
        print(json.dumps(collect(), sort_keys=True))
        return 0
    if command == "demo":
        recovery = recovery_audit(load_json("recovery_script.json"))
        publish = publish_audit(load_json("shelf_bytes.json"))
        print(
            "recovery_mismatches={recovery_mismatches} safe_corrupt={safe_corrupt} "
            "reorder={reorder}".format(
                recovery_mismatches=recovery["mismatches"],
                safe_corrupt=publish["safe_corrupt"],
                reorder=publish["reorder_outcome"],
            )
        )
        return 0
    print("usage: metrics | demo", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
