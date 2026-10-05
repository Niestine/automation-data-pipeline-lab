"""Command line: ``metrics`` prints measured crash-image counts; ``demo`` commits the sample."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "metrics"
    if command == "metrics":
        from recovery_lab.metrics import collect

        print(json.dumps(collect(), sort_keys=True))
        return 0
    if command == "demo":
        from recovery_lab.snapshot import fingerprint
        from recovery_lab.store import LedgerStore

        sample = Path(__file__).resolve().parents[2] / "examples" / "accounts.json"
        rows = json.loads(sample.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="rl04-demo-") as tmp:
            with LedgerStore(tmp) as store:
                for row in rows:
                    store.upsert(int(row["account_id"]), int(row["balance"]), str(row["email"]))
                    store.commit()
                print(fingerprint(store.ledger))
        return 0
    print("usage: metrics | demo", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
