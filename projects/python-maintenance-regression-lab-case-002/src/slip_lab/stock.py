"""Embedded synthetic corpus and the compatibility ledger it publishes."""

from __future__ import annotations

import json
from pathlib import Path

from slip_lab.differential import classify_inprocess
from slip_lab.errors import CorpusDriftError
from slip_lab.ledger import Ledger, sha256
from slip_lab.paths import CORPUS

QUOTE_ROW = b'""""|1\n'
QUOTE_MIN = b'""""'

SPECS: list[dict] = [
    {
        "id": "CASE-bare",
        "rel": "lexical/bare_quote.slip",
        "blob": b'A"|1\n',
        "origin": "lexical",
        "disposition": None,
    },
    {
        "id": "CASE-empty",
        "rel": "lexical/empty_doc.slip",
        "blob": b"",
        "origin": "lexical",
        "disposition": None,
    },
    {
        "id": "CASE-header",
        "rel": "fixed/header_lanes.slip",
        "blob": b"@slip|lane|qty\nL1|1\nL2|2\n",
        "origin": "fixed",
        "disposition": None,
    },
    {
        "id": "CASE-long",
        "rel": "lexical/long_lane.slip",
        "blob": b"A" * 180 + b"|1\n",
        "origin": "lexical",
        "disposition": None,
    },
    {
        "id": "CASE-quoted-pipe",
        "rel": "fixed/quoted_pipe.slip",
        "blob": b'"A|B"|1\n',
        "origin": "fixed",
        "disposition": None,
    },
    {
        "id": "CASE-short",
        "rel": "lexical/short_row.slip",
        "blob": b"@slip|lane|qty\nONLY\n",
        "origin": "lexical",
        "disposition": None,
    },
    {
        "id": "CASE-spaces",
        "rel": "lexical/spaces.slip",
        "blob": b"A |1\n",
        "origin": "lexical",
        "disposition": None,
    },
    {
        "id": "CASE-two-lanes",
        "rel": "fixed/two_lanes.slip",
        "blob": b"L1|1\nL2|2\nL3|3\n",
        "origin": "fixed",
        "disposition": None,
    },
    {
        "id": "CASE-unclosed",
        "rel": "lexical/unclosed.slip",
        "blob": b'"open|1\n',
        "origin": "lexical",
        "disposition": None,
    },
    {
        "id": "SLIP-001",
        "rel": "fixed/quote_escape.slip",
        "blob": QUOTE_ROW,
        "origin": "fixed",
        "disposition": "bug_legacy",
    },
    {
        "id": "SLIP-002",
        "rel": "fixed/trailing_bar.slip",
        "blob": b"A|1|\n",
        "origin": "fixed",
        "disposition": "accept_spec",
    },
    {
        "id": "SLIP-003",
        "rel": "fixed/nul_lane.slip",
        "blob": b"A\x00|1\n",
        "origin": "fixed",
        "disposition": "bug_legacy",
    },
    {
        "id": "SLIP-004",
        "rel": "fixed/cr_lane.slip",
        "blob": b"A|1\r\n",
        "origin": "fixed",
        "disposition": "malformed_probe",
    },
    {
        "id": "SLIP-005",
        "rel": "fixed/hash_lane.slip",
        "blob": b"#GATE|2\n",
        "origin": "fixed",
        "disposition": "bug_new",
    },
    {
        "id": "SLIP-006",
        "rel": "fixed/middle_empty.slip",
        "blob": b"A||B\n",
        "origin": "fixed",
        "disposition": "accept_compat",
    },
    {
        "id": "SLIP-007",
        "rel": "committed/reduced_quote.slip",
        "blob": QUOTE_MIN,
        "origin": "committed",
        "disposition": "bug_legacy",
        "parent_ids": ["SLIP-001"],
        "nonterminal": "field",
        "seed": 1,
    },
]


def defects() -> list[dict]:
    return [spec for spec in SPECS if spec["id"].startswith("SLIP-")]


def _jsonish(value: object):
    if value is None:
        return None
    return json.loads(json.dumps(value))


def build_rows() -> list[dict]:
    rows = []
    for spec in SPECS:
        if b"__HANG__" in spec["blob"]:
            raise RuntimeError("hang token is not part of the in-process corpus")
        outcome = classify_inprocess(spec["blob"])
        hardened_value = outcome.hardened.value if outcome.hardened.status == "ok" else None
        exc_type = None
        if outcome.kind == "CRASH":
            exc_type = outcome.legacy.exc_type or outcome.hardened.exc_type
        oracle = None
        stored_hardened = None
        if spec.get("disposition") == "accept_spec":
            oracle = _jsonish(hardened_value)
            stored_hardened = oracle
        elif spec.get("disposition") in {"bug_legacy", "bug_new", "malformed_probe"}:
            stored_hardened = _jsonish(hardened_value)
        row = {
            "byte_length": len(spec["blob"]),
            "corpus_file": spec["rel"],
            "disposition": spec.get("disposition"),
            "exc_type": exc_type,
            "hardened_value": stored_hardened,
            "id": spec["id"],
            "nonterminal": spec.get("nonterminal"),
            "oracle": oracle,
            "origin": spec["origin"],
            "outcome": outcome.kind,
            "parent_ids": list(spec.get("parent_ids", [])),
            "seed": spec.get("seed"),
            "sha256": sha256(spec["blob"]),
            "side": outcome.side,
            "timeout_s": None,
            "truncated": False,
        }
        rows.append(row)
    rows.sort(key=lambda item: item["id"])
    return rows


def publish(corpus: Path = CORPUS) -> None:
    """Write embedded blobs and the ledger when disk content differs.

    The check and the tests never call this on the real corpus; they
    compare against it. Run it by hand after a reviewed ledger change.
    """

    ledger_path = Path(corpus) / "ledger.jsonl"
    for spec in SPECS:
        path = Path(corpus) / spec["rel"]
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.read_bytes() if path.is_file() else None
        if current != spec["blob"]:
            path.write_bytes(spec["blob"])
    ledger = Ledger()
    for row in build_rows():
        ledger.add(row)
    rendered = ledger.dumps()
    if not ledger_path.is_file() or ledger_path.read_text(encoding="utf-8") != rendered:
        ledger.write(ledger_path)


def load_published(corpus: Path = CORPUS) -> list[dict]:
    """Ledger rows from disk, after every corpus file matches its bytes."""

    for spec in SPECS:
        path = Path(corpus) / spec["rel"]
        if not path.is_file():
            raise CorpusDriftError(f"corpus file missing: {spec['rel']}")
        if path.read_bytes() != spec["blob"]:
            raise CorpusDriftError(f"corpus file drift: {spec['rel']}")
    ledger_path = Path(corpus) / "ledger.jsonl"
    if not ledger_path.is_file():
        raise CorpusDriftError("ledger file missing")
    try:
        return Ledger.load(ledger_path).rows
    except ValueError as exc:
        raise CorpusDriftError(f"ledger file is not JSONL: {exc}") from exc
