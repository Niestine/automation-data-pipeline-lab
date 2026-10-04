import unittest

import helpers  # noqa: F401

from slip_lab.differential import classify_inprocess
from slip_lab.errors import GateError, LedgerConflict, LedgerError
from slip_lab.gate import require
from slip_lab.hardened import parse as hardened_parse
from slip_lab.ledger import Ledger, sha256
from slip_lab.model import DISPOSITIONS
from slip_lab.stock import SPECS, build_rows


def _row(blob: bytes, disposition: str | None, outcome: str = "DISAGREE") -> dict:
    return {
        "byte_length": len(blob),
        "corpus_file": "memory.slip",
        "disposition": disposition,
        "exc_type": None,
        "hardened_value": None,
        "id": "mem",
        "nonterminal": None,
        "oracle": None,
        "origin": "fixed",
        "outcome": outcome,
        "parent_ids": [],
        "seed": None,
        "sha256": sha256(blob),
        "side": "both" if outcome == "DISAGREE" else None,
        "timeout_s": None,
        "truncated": False,
    }


class LedgerTests(unittest.TestCase):
    def test_ledger_requires_label(self):
        ledger = Ledger()
        with self.assertRaises(LedgerError):
            ledger.add(_row(b"bare", None))
        round_trip = Ledger()
        for label in DISPOSITIONS:
            round_trip.add(_row(label.encode("ascii"), label))
        restored = Ledger()
        for line in round_trip.dumps().splitlines():
            import json

            restored.add(json.loads(line))
        self.assertEqual([row["disposition"] for row in restored.rows], list(DISPOSITIONS))
        agree = _row(b"ok", None, outcome="AGREE")
        ledger.add(agree)
        self.assertEqual(len(ledger.rows), 1)

    def test_upsert_is_idempotent(self):
        ledger = Ledger()
        row = _row(b"same", "bug_legacy")
        ledger.add(row)
        ledger.add(dict(row))
        self.assertEqual(len(ledger.rows), 1)
        changed = dict(row)
        changed["disposition"] = "bug_new"
        with self.assertRaises(LedgerConflict):
            ledger.add(changed)

    def test_accept_compat_pins_legacy(self):
        rows = {row["id"]: row for row in build_rows()}
        compat = rows["SLIP-006"]
        blob = next(spec["blob"] for spec in SPECS if spec["id"] == "SLIP-006")
        require(compat, classify_inprocess(blob))

        def drifted(raw):
            value = hardened_parse(raw)
            _kind, header, records = value
            return ("records", header, tuple(records) + (("EXTRA",),))

        with self.assertRaises(GateError):
            require(compat, classify_inprocess(blob, hardened_fn=drifted))

        spec_row = rows["SLIP-002"]
        spec_blob = next(spec["blob"] for spec in SPECS if spec["id"] == "SLIP-002")
        require(spec_row, classify_inprocess(spec_blob))

        def wrong_oracle(_raw):
            return ("records", None, (("NOPE",),))

        with self.assertRaises(GateError):
            require(spec_row, classify_inprocess(spec_blob, hardened_fn=wrong_oracle))


if __name__ == "__main__":
    unittest.main()
