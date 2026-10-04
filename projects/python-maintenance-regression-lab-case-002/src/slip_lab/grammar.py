"""Record grammar used to learn splice fragments.

Fragments are cut by this grammar, not by the parser under test.
"""

from __future__ import annotations

from slip_lab.dialect import grammar_table


def parse_tree(blob: bytes) -> dict | None:
    table = grammar_table(blob)
    if table is None:
        return None
    records = []
    for raw_fields in table:
        children = []
        for index, raw in enumerate(raw_fields):
            if index:
                children.append({"nt": "separator", "text": "|"})
            kind = "quoted_field" if raw.startswith('"') else "field"
            children.append({"nt": kind, "text": raw})
        records.append({"nt": "record", "text": "|".join(raw_fields), "children": children})
    return {"nt": "document", "records": records}


def start_symbol(blob: bytes) -> str | None:
    tree = parse_tree(blob)
    if tree is None:
        return None
    return tree["nt"]
