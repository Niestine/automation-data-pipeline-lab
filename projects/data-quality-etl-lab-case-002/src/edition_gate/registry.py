"""Schema registry: SMO log, publication states, FULL_TRANSITIVE checks.

A reader default fills a column the writer omitted. It does not rewrite a
value the writer encoded. Rename is one field only when the new column
carries the old name as an alias; drop plus add is a different operator.
"""

from __future__ import annotations

from typing import Optional

from .fingerprint import parse_fingerprint, resolution_fingerprint
from .model import Schema


OPTIONAL_CHAIN = ("absent", "delete_only", "public")
REQUIRED_CHAIN = ("absent", "delete_only", "write_only", "public")

_WIDEN = {
    "string": {"string"},
    "boolean": {"boolean"},
    "date": {"date"},
    "datetime": {"datetime"},
    "integer": {"integer", "long", "double", "decimal"},
    "long": {"long", "double"},
    "double": {"double"},
    "decimal": {"decimal"},
}


def promotable(writer_type: str, reader_type: str) -> bool:
    return reader_type in _WIDEN.get(writer_type, set())


def _unique(items: list) -> list:
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _find(parent: dict, name: str) -> str:
    parent.setdefault(name, name)
    if parent[name] != name:
        parent[name] = _find(parent, parent[name])
    return parent[name]


def equivalence_groups(operators: list) -> dict:
    parent = {}
    for op in operators:
        if op.get("op") != "rename_column":
            continue
        left = _find(parent, op["from"])
        right = _find(parent, op["to"])
        if left != right:
            parent[right] = left
    groups = {}
    for name in list(parent):
        root = _find(parent, name)
        groups.setdefault(root, set()).add(name)
    return groups


def names_equivalent(left: str, right: str, operators: list) -> bool:
    if left == right:
        return True
    for members in equivalence_groups(operators).values():
        if left in members and right in members:
            return True
    return False


def rewrite_names(column_name: str, operators: list) -> dict:
    """Historical names that land on this column, newest first, as UNION ALL."""
    sources = [column_name]
    for op in reversed(operators):
        kind = op.get("op")
        if kind == "rename_column" and op.get("to") in sources:
            sources.append(op["from"])
        elif kind == "merge_columns" and op.get("target") in sources:
            sources.extend(op.get("sources") or [])
        elif kind == "split_column" and column_name in (op.get("targets") or []):
            sources.append(op.get("source"))
    ordered = []
    for name in sources:
        if name and name not in ordered:
            ordered.append(name)
    return {"column": column_name, "form": "UNION ALL", "sources": ordered}


def assess_operators(operators: list, publication_changes: Optional[list] = None) -> list:
    reasons = []
    drops = [op for op in operators if op.get("op") == "drop_column"]
    adds = [op for op in operators if op.get("op") == "add_column"]
    drop_names = {op.get("name") for op in drops}
    if drops and adds:
        for add in adds:
            source = (add.get("transform") or {}).get("from")
            if source not in drop_names:
                reasons.append("not_information_preserving")
                break
    elif drops:
        changes = publication_changes or []
        for drop in drops:
            change = next((item for item in changes if item[0] == drop.get("name")), None)
            legal_protocol = change is not None and change[1] == "delete_only" and change[2] == "absent"
            if not legal_protocol:
                reasons.append("not_information_preserving")
                break
    for op in operators:
        transform = op.get("transform") or {}
        if op.get("op") == "merge_columns" and "provenance" not in transform:
            reasons.append("not_information_preserving")
        if op.get("op") == "split_column":
            if not op.get("shared_key") and "provenance" not in transform:
                reasons.append("not_information_preserving")
    return _unique(reasons)


def _chain(required: bool) -> tuple:
    return REQUIRED_CHAIN if required else OPTIONAL_CHAIN


def one_step(old: str, new: str, required: bool) -> bool:
    chain = _chain(required)
    if old not in chain or new not in chain:
        return False
    return abs(chain.index(old) - chain.index(new)) == 1


def _writer_columns(schema: Schema) -> list:
    return [col for col in schema.columns if col.publication != "absent" and not col.virtual]


def _match_writer(reader_col, writer_cols: list, operators: list):
    for writer_col in writer_cols:
        if writer_col.name == reader_col.name or writer_col.name in reader_col.aliases:
            return writer_col
        if names_equivalent(writer_col.name, reader_col.name, operators):
            return writer_col
        for alias in reader_col.aliases:
            if names_equivalent(writer_col.name, alias, operators):
                return writer_col
    return None


def resolves(writer: Schema, reader: Schema, operators: list, direction: str) -> bool:
    """Whether `reader` can read rows written with `writer`.

    Backward uses Avro widening from the writer type to the reader type.
    Forward is a lab relaxation, not the Avro or Confluent rule: an older
    reader with the narrower type may read a widened writer, because in CSV
    every field is text and an out-of-range value becomes a type_error cell
    in that reader instead of a failed file. Avro FULL would reject an
    int-to-long change. Narrowing fails both ways.
    """
    written = _writer_columns(writer)
    for reader_col in reader.columns:
        if reader_col.virtual:
            if not reader_col.has_default:
                return False
            continue
        if reader_col.publication != "public":
            continue
        found = _match_writer(reader_col, written, operators)
        if found is None:
            if not reader_col.has_default:
                return False
            continue
        if direction == "backward":
            if not promotable(found.datatype, reader_col.datatype):
                return False
        elif not promotable(reader_col.datatype, found.datatype):
            return False
    return True


def full_pair(older: Schema, newer: Schema, operators: list) -> bool:
    backward = resolves(older, newer, operators, "backward")
    forward = resolves(newer, older, operators, "forward")
    return backward and forward


def full_transitive(candidate: Schema, retained: list, operators: list) -> bool:
    """FULL against every retained writer, not only the neighbor."""
    for older in retained:
        if not full_pair(older, candidate, operators):
            return False
    return True


def explain_resolution(writer: Schema, reader: Schema, operators: list) -> dict:
    written = _writer_columns(writer)
    plan = {}
    used = set()
    for reader_col in reader.columns:
        if reader_col.virtual:
            plan[reader_col.name] = {"source": "virtual", "value": reader_col.default}
            continue
        if reader_col.publication != "public":
            continue
        found = _match_writer(reader_col, written, operators)
        if found is None:
            plan[reader_col.name] = {
                "source": "default" if reader_col.has_default else "missing",
                "value": reader_col.default if reader_col.has_default else None,
            }
        else:
            used.add(found.name)
            plan[reader_col.name] = {"source": "writer", "writer_field": found.name}
    ignored = [col.name for col in written if col.name not in used]
    return {"columns": plan, "ignored_writer_fields": ignored}


class CanonicalStore:
    def __init__(self, rows: Optional[list] = None) -> None:
        self.rows = {}
        for row in rows or []:
            self.upsert(row)

    def upsert(self, row: dict) -> None:
        self.rows[row["canonical_id"]] = dict(row)

    def sorted_rows(self) -> list:
        return [self.rows[key] for key in sorted(self.rows)]


class Registry:
    def __init__(self) -> None:
        self.versions = []
        self.operators = []
        self.store = CanonicalStore()
        self.backfill_done = set()
        self.reorg_done = set()
        self.applied_batches = {}

    def latest(self) -> Optional[Schema]:
        if not self.versions:
            return None
        return self.versions[-1]

    def get(self, number: int) -> Optional[Schema]:
        for version in self.versions:
            if version.number == number:
                return version
        return None

    def rewrite(self, column_name: str) -> dict:
        return rewrite_names(column_name, self.operators)

    def backfill(self, name: str) -> dict:
        current = self.latest()
        if current is None:
            return {"ok": False, "reasons": ["version_sequence"]}
        col = current.column(name)
        if col is None or col.publication != "write_only":
            return {"ok": False, "reasons": ["backfill_required"]}
        if not col.has_default:
            return {"ok": False, "reasons": ["backfill_required"]}
        filled = 0
        for row in self.store.rows.values():
            if name not in row or row[name] is None:
                row[name] = col.default
                filled += 1
        self.backfill_done.add(name)
        return {"ok": True, "reasons": [], "filled": filled}

    def reorganize_drop(self, name: str) -> dict:
        current = self.latest()
        if current is None:
            return {"ok": False, "reasons": ["reorg_required"]}
        col = current.column(name)
        if col is None or col.publication != "delete_only":
            return {"ok": False, "reasons": ["reorg_required"]}
        for row in self.store.rows.values():
            row.pop(name, None)
        self.reorg_done.add(name)
        return {"ok": True, "reasons": []}

    def register(self, source: Schema, operators: Optional[list] = None) -> dict:
        operators = list(operators or [])
        reasons = []
        if source.number != len(self.versions) + 1:
            reasons.append("version_sequence")
        previous = self.latest()
        changes = []
        if previous is None:
            for col in source.columns:
                if not col.virtual and col.publication != "public":
                    reasons.append("publication_jump")
            if operators:
                reasons.append("operator_missing")
        else:
            changes = self._publication_diff(previous, source, operators)
            # One column-state change per edition. A merge or split moves its
            # source and target columns together, so it is exempt from the
            # count; each column must still move one step.
            if len(changes) > 1 and not self._merge_split_only(operators):
                reasons.append("publication_jump")
            for name, old, new, col, required in changes:
                if not one_step(old, new, required):
                    reasons.append("publication_jump")
                if old == "write_only" and new == "public" and name not in self.backfill_done:
                    reasons.append("backfill_required")
                if old == "delete_only" and new == "absent" and name not in self.reorg_done:
                    reasons.append("reorg_required")
            reasons.extend(self._missed_alias(previous, source, operators))
            reasons.extend(self._operator_rules(previous, source, operators))
            proposed = self.operators + [{**op, "version": source.number} for op in operators]
            if not self._compatible(source, proposed):
                reasons.append("compatibility_rejected")
        reasons.extend(assess_operators(operators, [(item[0], item[1], item[2]) for item in changes]))
        reasons = _unique(reasons)
        if reasons:
            return {
                "ok": False,
                "reasons": reasons,
                "version": None,
                "parse_fingerprint": None,
                "resolution_fingerprint": None,
            }
        self.versions.append(source)
        for op in operators:
            self.operators.append({**op, "version": source.number})
        for name, old, new, _col, _required in changes:
            if new == "write_only" and old == "delete_only":
                self.backfill_done.discard(name)
            if new == "delete_only":
                self.reorg_done.discard(name)
        return {
            "ok": True,
            "reasons": [],
            "version": source,
            "parse_fingerprint": parse_fingerprint(source),
            "resolution_fingerprint": resolution_fingerprint(source),
        }

    def _compatible(self, candidate: Schema, operators: list) -> bool:
        for older in self.versions:
            if not resolves(older, candidate, operators, "backward"):
                return False
            if not resolves(candidate, older, operators, "forward"):
                return False
        return True

    def _merge_split_only(self, operators: list) -> bool:
        if not operators:
            return False
        return all(op.get("op") in {"merge_columns", "split_column"} for op in operators)

    def _names_after_rename(self, previous: Schema, operators: list) -> dict:
        state = {col.name: col for col in previous.columns}
        for op in operators:
            if op.get("op") == "rename_column" and op.get("from") in state:
                state[op["to"]] = state.pop(op["from"])
        return state

    def _publication_diff(self, previous: Schema, source: Schema, operators: list) -> list:
        state = self._names_after_rename(previous, operators)
        changes = []
        seen = set()
        for col in source.columns:
            seen.add(col.name)
            old_col = state.get(col.name)
            old = "absent" if old_col is None else old_col.publication
            if old != col.publication:
                changes.append((col.name, old, col.publication, col, col.required))
        for name, old_col in state.items():
            if name not in seen and old_col.publication != "absent":
                changes.append((name, old_col.publication, "absent", None, old_col.required))
        return changes

    def _missed_alias(self, previous: Schema, source: Schema, operators: list) -> list:
        live = [col for col in source.columns if col.publication != "absent"]
        reasons = []
        for col in previous.columns:
            if col.publication != "public":
                continue
            represented = any(item.name == col.name or col.name in item.aliases for item in live)
            explicit_drop = any(
                op.get("op") == "drop_column" and op.get("name") == col.name for op in operators
            )
            if represented or explicit_drop:
                continue
            reasons.append("missed_alias")
        return reasons

    def _operator_rules(self, previous: Schema, source: Schema, operators: list) -> list:
        reasons = []
        prior = self._names_after_rename(previous, operators)
        for col in source.columns:
            old = prior.get(col.name)
            if old is None and col.publication != "absent":
                if not any(op.get("op") == "add_column" and op.get("name") == col.name for op in operators):
                    reasons.append("operator_missing")
                continue
            if old is None:
                continue
            if old.datatype != col.datatype:
                typed = [
                    op
                    for op in operators
                    if op.get("op") == "set_type" and op.get("name") in {col.name, old.name}
                ]
                if not typed:
                    reasons.append("operator_missing")
                elif not promotable(old.datatype, col.datatype):
                    reasons.append("compatibility_rejected")
            default_changed = old.has_default != col.has_default or old.default != col.default
            if default_changed:
                if not any(op.get("op") == "set_default" and op.get("name") == col.name for op in operators):
                    reasons.append("operator_missing")
        return reasons
