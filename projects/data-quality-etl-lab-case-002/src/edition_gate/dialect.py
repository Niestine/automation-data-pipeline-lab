"""Stage 2. Use the contract dialect, or search and abstain on a real tie.

Q = P · T from van den Burg, Nazábal, and Sutton. alpha = 10^-3 keeps a
single-column file from scoring zero. beta = 10^-10 keeps an untyped file
from zeroing the product. The Python sniffer is recorded and never breaks a tie.
"""

from __future__ import annotations

import csv
from fractions import Fraction
import re
from typing import Optional

from .records import detect_line_break, parse_text


ALPHA = Fraction(1, 1000)
BETA = Fraction(1, 10**10)

_DELIM_PRIORITY = {",": 0, ";": 1, "\t": 2, "|": 3, " ": 4}
_DELIM_CANDIDATES = [",", ";", "\t", "|", " "]

_TYPE_PATTERNS = (
    re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?%?"),
    re.compile(r"https?://\S+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"[$£€][+-]?\d+(?:\.\d{2})?"),
    re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"),
    re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"),
    re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}"),
    re.compile(r"[A-Za-z0-9]+(?:[ ._-][A-Za-z0-9]+)*"),
    re.compile(r"(?i:n/?a|null|none|nan)"),
)


def pattern_score(lengths: list) -> Fraction:
    if not lengths:
        return Fraction(0)
    counts = {}
    for length in lengths:
        counts[length] = counts.get(length, 0) + 1
    total = Fraction(0)
    for length, count in counts.items():
        numerator = max(ALPHA, Fraction(length - 1))
        total += Fraction(count) * numerator / Fraction(length)
    return total / Fraction(len(counts))


def cell_is_typed(value: str) -> bool:
    if value == "":
        return True
    return any(pattern.fullmatch(value) for pattern in _TYPE_PATTERNS)


def type_score(rows: list) -> Fraction:
    total = 0
    matched = 0
    for row in rows:
        for cell in row:
            total += 1
            if cell_is_typed(cell):
                matched += 1
    if total == 0:
        return BETA
    return max(BETA, Fraction(matched, total))


def _sniff(text: str) -> Optional[dict]:
    sample = text[:8192]
    try:
        found = csv.Sniffer().sniff(sample, delimiters=",;\t| ")
    except csv.Error:
        return None
    return {
        "delimiter": found.delimiter,
        "quote": found.quotechar or "",
        "skip_initial_space": bool(found.skipinitialspace),
    }


def _score_dialect(text: str, delimiter: str, quote: str, escape: str, skip_rows: int, line_break: str):
    try:
        parsed = parse_text(text, delimiter, quote, escape, line_break)
    except ValueError:
        return None
    # Rows with bad_quote are scored as parsed. An unclosed quote swallows the
    # rest of the file into one row, which the pattern score already punishes.
    usable = parsed[skip_rows:]
    matrix = tuple(tuple(row["fields"]) for row in usable)
    pattern = pattern_score([len(row["fields"]) for row in usable])
    return {"pattern": pattern, "matrix": matrix}


def _candidates(text: str) -> list:
    delims = [item for item in _DELIM_CANDIDATES if item in text] or [","]
    quotes = [""]
    if '"' in text:
        quotes.append('"')
    if "'" in text:
        quotes.append("'")
    escapes = [""]
    if "\\" in text:
        escapes.append("\\")
    line_break = detect_line_break(text)
    found = []
    for delimiter in delims:
        for quote in quotes:
            if quote and quote == delimiter:
                continue
            for escape in escapes:
                if escape and escape == delimiter:
                    continue
                for skip_rows in (0, 1):
                    found.append(
                        {
                            "delimiter": delimiter,
                            "quote": quote,
                            "escape": escape,
                            "skip_rows": skip_rows,
                            "line_break": line_break,
                            "header": "present",
                            "header_row_count": 1,
                        }
                    )
    return found


def _represent(cluster: list) -> dict:
    def sort_key(item):
        dialect = item["dialect"]
        return (
            dialect["escape"] != "",
            dialect["quote"] != "",
            _DELIM_PRIORITY.get(dialect["delimiter"], 9),
            dialect["skip_rows"],
        )

    return sorted(cluster, key=sort_key)[0]


def detect_dialect(text: str, contract: Optional[dict] = None) -> dict:
    sniffer = _sniff(text)
    if contract and contract.get("delimiter"):
        dialect = {
            "delimiter": contract["delimiter"],
            "quote": contract.get("quote", '"'),
            "escape": contract.get("escape", ""),
            "skip_rows": contract.get("skip_rows", 0),
            "line_break": contract.get("line_break") or detect_line_break(text),
            "header": contract.get("header", "present"),
            "header_row_count": contract.get("header_row_count", 1),
            "charset": contract.get("charset"),
        }
        return {
            "status": "selected",
            "source": "contract",
            "dialect": dialect,
            "candidates": [],
            "sniffer": sniffer,
            "tie_broken": False,
        }
    best = []
    best_score = None
    for candidate in _candidates(text):
        scored = _score_dialect(
            text,
            candidate["delimiter"],
            candidate["quote"],
            candidate["escape"],
            candidate["skip_rows"],
            candidate["line_break"],
        )
        if scored is None:
            continue
        pattern = scored["pattern"]
        if best_score is not None and pattern < best_score:
            continue
        typed = type_score([list(row) for row in scored["matrix"]])
        score = pattern * typed
        entry = {
            "dialect": candidate,
            "score": score,
            "pattern": pattern,
            "type": typed,
            "matrix": scored["matrix"],
        }
        if best_score is None or score > best_score:
            best = [entry]
            best_score = score
        elif score == best_score:
            best.append(entry)
    if not best:
        return {
            "status": "dialect_tie",
            "source": "search",
            "dialect": None,
            "candidates": [],
            "sniffer": sniffer,
            "tie_broken": False,
        }
    clusters = []
    for entry in best:
        placed = False
        for cluster in clusters:
            if cluster[0]["matrix"] == entry["matrix"]:
                cluster.append(entry)
                placed = True
                break
        if not placed:
            clusters.append([entry])
    representatives = [_represent(cluster) for cluster in clusters]
    payload = []
    for entry in representatives:
        payload.append(
            {
                "delimiter": entry["dialect"]["delimiter"],
                "quote": entry["dialect"]["quote"],
                "escape": entry["dialect"]["escape"],
                "skip_rows": entry["dialect"]["skip_rows"],
                "line_break": entry["dialect"]["line_break"],
                "score": str(entry["score"]),
            }
        )
    if len(clusters) == 1:
        chosen = representatives[0]["dialect"]
        return {
            "status": "selected",
            "source": "search",
            "dialect": chosen,
            "candidates": payload,
            "sniffer": sniffer,
            "tie_broken": len(best) > 1,
        }
    return {
        "status": "dialect_tie",
        "source": "search",
        "dialect": None,
        "candidates": payload,
        "sniffer": sniffer,
        "tie_broken": False,
    }
