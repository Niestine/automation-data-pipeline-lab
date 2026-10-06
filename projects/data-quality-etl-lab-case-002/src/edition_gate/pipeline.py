"""Ingest a parts-catalog file through the six gates and the schema registry."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from .cells import parse_cell, promote
from .decode import decode_bytes
from .dialect import detect_dialect
from .fingerprint import parse_fingerprint, resolution_fingerprint
from .link import link_rows
from .model import canonical_json, column, dialect as make_dialect, schema as make_schema
from .normalize import length_report, match_key, unidata_version
from .records import parse_text, syntactic_empty
from .registry import Registry, names_equivalent, resolves


LOGGER = logging.getLogger("edition_gate")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def read_with_retry(path, attempts: int = 3, sleeper=None, opener=None) -> bytes:
    """Retry OSError. The default sleeper records the schedule and does not wait."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if sleeper is None:
        sleeper = lambda _delay: None
    if opener is None:
        opener = lambda item: open(item, "rb")
    delay = 0.01
    attempt = 1
    while True:
        try:
            with opener(path) as handle:
                return handle.read()
        except OSError:
            if attempt >= attempts:
                raise
            LOGGER.info("read_retry attempt=%s delay=%s", attempt, delay)
            sleeper(delay)
            delay *= 2
            attempt += 1


def _event(events: list, stage: str, name: str, **detail) -> None:
    event = {"stage": stage, "name": name}
    event.update(detail)
    events.append(event)
    extra = " ".join("{0}={1}".format(key, value) for key, value in detail.items())
    if extra:
        LOGGER.info("%s %s %s", stage, name, extra)
    else:
        LOGGER.info("%s %s", stage, name)


def _quarantine(reason: str, events: list, **extra) -> dict:
    _event(events, "ingest", "quarantine", reason=reason)
    result = {
        "status": "quarantine",
        "reason": reason,
        "reasons": [reason],
        "rows": [],
        "quarantined_rows": [],
        "links": [],
        "review": [],
        "events": events,
        "headers": [],
        "dialect_source": None,
        "dialect": None,
        "sniffer": None,
        "candidates": [],
        "charset_overridden_by_bom": False,
    }
    result.update(extra)
    return result


def _header_matches(titles: list, col) -> bool:
    for title in titles:
        key = match_key(title)
        if key == match_key(col.name):
            return True
        if any(key == match_key(alias) for alias in col.aliases):
            return True
        if any(key == match_key(text) for text, _language in col.titles):
            return True
    return False


def _must_emit(col) -> bool:
    return (not col.virtual) and col.required and col.publication in {"public", "write_only"}


def _project_target(writer_col, reader, operators: list):
    for col in reader.columns:
        if col.virtual or col.publication != "public":
            continue
        if col.name == writer_col.name or writer_col.name in col.aliases:
            return col
        if names_equivalent(col.name, writer_col.name, operators):
            return col
    return None


def _stored_text(cell: dict) -> object:
    if cell.get("nfc") is not None and not cell.get("errors"):
        return cell["nfc"]
    return cell.get("value")


def _link_record(row: dict) -> dict:
    values = {}
    for name in ("supplier_id", "sku", "brand", "size", "title", "organization"):
        cell = row["cells"].get(name)
        values[name] = None if cell is None else cell.get("value")
    values["row_id"] = str(row["row_number"])
    values["row_number"] = row["row_number"]
    return values


def _new_id(row: dict) -> str:
    supplier = row.get("supplier_id") or "NA"
    sku = row.get("sku")
    if sku:
        return "P-{0}-{1}".format(supplier, sku)
    return "P-{0}-R{1}".format(supplier, row["row_number"])


def _batch_key(payload: bytes, writer_version: int, reader, settings: dict) -> str:
    material = canonical_json(
        {
            "payload": hashlib.sha256(payload).hexdigest(),
            "writer": writer_version,
            "reader": resolution_fingerprint(reader),
            "settings": settings,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def ingest_bytes(
    registry: Registry,
    payload: bytes,
    *,
    writer_version: int,
    declared_charset: Optional[str] = None,
    search: bool = False,
    error_budget: Optional[int] = None,
    link_config: Optional[dict] = None,
    dry_run: bool = False,
    link: bool = True,
) -> dict:
    events = []
    reader = registry.latest()
    writer = registry.get(writer_version)
    if reader is None or writer is None:
        return _quarantine(
            "schema_resolution",
            events,
            writer_version=writer_version,
            reader_version=None if reader is None else reader.number,
        )
    # Every argument that can change the result is part of the key, so a
    # corrected charset or a linked re-run is not answered from the cache.
    cache_key = _batch_key(
        payload,
        writer_version,
        reader,
        {
            "declared_charset": declared_charset,
            "search": search,
            "error_budget": error_budget,
            "link": link,
            "link_config": link_config or {},
        },
    )
    if not dry_run and cache_key in registry.applied_batches:
        _event(events, "ingest", "idempotent_replay")
        cached = json.loads(json.dumps(registry.applied_batches[cache_key]))
        cached["events"] = events + cached.get("events", [])
        cached["idempotent_replay"] = True
        return cached
    charset = declared_charset if declared_charset is not None else writer.dialect.charset
    decoded = decode_bytes(payload, charset)
    if decoded.get("charset_overridden_by_bom"):
        _event(events, "decode", "charset_overridden_by_bom", encoding=decoded.get("encoding"))
    if not decoded["ok"]:
        return _quarantine(
            decoded["reason"],
            events,
            writer_version=writer.number,
            reader_version=reader.number,
            encoding=decoded.get("encoding"),
            charset_overridden_by_bom=decoded.get("charset_overridden_by_bom", False),
        )
    if reader.number - writer.number > 1:
        return _quarantine(
            "schema_too_old",
            events,
            writer_version=writer.number,
            reader_version=reader.number,
            charset_overridden_by_bom=decoded.get("charset_overridden_by_bom", False),
            encoding=decoded.get("encoding"),
        )
    if not resolves(writer, reader, registry.operators, "backward"):
        return _quarantine(
            "schema_resolution",
            events,
            writer_version=writer.number,
            reader_version=reader.number,
            charset_overridden_by_bom=decoded.get("charset_overridden_by_bom", False),
            encoding=decoded.get("encoding"),
        )
    text = decoded["text"]
    if search:
        decision = detect_dialect(text)
    else:
        decision = detect_dialect(text, contract=writer.dialect.to_plain())
    if decision["status"] != "selected":
        return _quarantine(
            "dialect_tie",
            events,
            writer_version=writer.number,
            reader_version=reader.number,
            dialect_source=decision["source"],
            candidates=decision["candidates"],
            sniffer=decision["sniffer"],
            charset_overridden_by_bom=decoded.get("charset_overridden_by_bom", False),
            encoding=decoded.get("encoding"),
        )
    chosen = decision["dialect"]
    parsed = parse_text(
        text,
        chosen["delimiter"],
        chosen.get("quote") or "",
        chosen.get("escape") or "",
        chosen.get("line_break") or "lf",
    )
    table = _cut_table(parsed, chosen, writer)
    if table.get("reason"):
        return _quarantine(
            table["reason"],
            events,
            writer_version=writer.number,
            reader_version=reader.number,
            dialect_source=decision["source"],
            dialect=_public_dialect(chosen),
            sniffer=decision["sniffer"],
            charset_overridden_by_bom=decoded.get("charset_overridden_by_bom", False),
            encoding=decoded.get("encoding"),
        )
    missing = _missing_required(writer, table["assignments"])
    if missing or table["duplicates"]:
        return _quarantine(
            "schema_resolution",
            events,
            writer_version=writer.number,
            reader_version=reader.number,
            dialect_source=decision["source"],
            dialect=_public_dialect(chosen),
            sniffer=decision["sniffer"],
            missing_columns=missing,
            duplicate_columns=table["duplicates"],
            charset_overridden_by_bom=decoded.get("charset_overridden_by_bom", False),
            encoding=decoded.get("encoding"),
        )
    rows = []
    quarantined_rows = []
    for record in table["data_rows"]:
        reasons = []
        if record["reason"]:
            reasons.append(record["reason"])
        if len(record["fields"]) != table["width"] and "trailing_delimiter" not in reasons:
            reasons.append("ragged_row")
        if reasons:
            quarantined_rows.append(
                {
                    "source_row": record["source_row"],
                    "row_number": record["row_number"],
                    "reasons": reasons,
                }
            )
            continue
        built = _build_row(record, table, reader, registry.operators)
        rows.append(built)
    error_count = sum(1 for row in rows for cell in row["cells"].values() if cell["errors"])
    if error_budget is not None and error_count > error_budget:
        return _quarantine(
            "error_budget",
            events,
            writer_version=writer.number,
            reader_version=reader.number,
            dialect_source=decision["source"],
            dialect=_public_dialect(chosen),
            sniffer=decision["sniffer"],
            charset_overridden_by_bom=decoded.get("charset_overridden_by_bom", False),
            encoding=decoded.get("encoding"),
            error_count=error_count,
        )
    review = []
    links = []
    if link:
        incoming = [_link_record(row) for row in rows]
        linked = link_rows(incoming, registry.store.sorted_rows(), link_config)
        for item in linked["links"]:
            links.append(
                {
                    "row_number": int(item["row_id"]),
                    "canonical_id": item["canonical_id"],
                    "method": item["method"],
                    "score": item["score"],
                }
            )
        for item in linked["reviews"]:
            review.append(
                {
                    "kind": item["kind"],
                    "row_number": int(item["row_id"]),
                    "canonical_id": item.get("canonical_id"),
                    "canonical_ids": item.get("canonical_ids"),
                    "score": item.get("score"),
                }
            )
        # Any linker review holds the row out of the canonical table. A row
        # that is only a compatibility fold of an existing part is not a new
        # part either.
        linked_ids = {item["row_number"] for item in links}
        reviewed_ids = {item["row_number"] for item in review}
        for row in rows:
            if row["row_number"] in linked_ids or row["row_number"] in reviewed_ids:
                continue
            record = _link_record(row)
            new_id = _new_id(record)
            links.append(
                {
                    "row_number": row["row_number"],
                    "canonical_id": new_id,
                    "method": "new",
                    "score": None,
                }
            )
    pair_folds = {item["row_number"] for item in review if item["kind"] == "compatibility_fold"}
    for row in rows:
        if "compatibility_fold" in row["flags"] and row["row_number"] not in pair_folds:
            review.append({"kind": "compatibility_fold", "row_number": row["row_number"]})
    links.sort(key=lambda item: (item["row_number"], item["canonical_id"]))
    review.sort(key=lambda item: (item["row_number"], item["kind"]))
    status = "accepted_with_errors" if error_count else "accepted"
    _event(events, "ingest", status, rows=len(rows))
    result = {
        "status": status,
        "reason": None,
        "reasons": [],
        "rows": rows,
        "quarantined_rows": quarantined_rows,
        "links": links,
        "review": review,
        "events": events,
        "headers": table["headers"],
        "dialect_source": decision["source"],
        "dialect": _public_dialect(chosen),
        "sniffer": decision["sniffer"],
        "candidates": decision["candidates"],
        "charset_overridden_by_bom": decoded.get("charset_overridden_by_bom", False),
        "encoding": decoded.get("encoding"),
        "writer_version": writer.number,
        "reader_version": reader.number,
        "error_count": error_count,
        "idempotent_replay": False,
        "parse_fingerprint": parse_fingerprint(reader),
        "resolution_fingerprint": resolution_fingerprint(reader),
    }
    if not dry_run:
        _apply_store(registry, rows, links)
        registry.applied_batches[cache_key] = json.loads(json.dumps(result))
    return result


def _public_dialect(chosen: dict) -> dict:
    return {
        "delimiter": chosen.get("delimiter"),
        "quote": chosen.get("quote", ""),
        "escape": chosen.get("escape", ""),
        "skip_rows": chosen.get("skip_rows", 0),
        "header_row_count": chosen.get("header_row_count", 1),
        "line_break": chosen.get("line_break"),
        "header": chosen.get("header", "present"),
    }


def _writer_file_columns(writer) -> list:
    return [col for col in writer.columns if not col.virtual and col.publication != "absent"]


def _cut_table(parsed: list, chosen: dict, writer) -> dict:
    skip = int(chosen.get("skip_rows") or 0)
    header_mode = chosen.get("header", "present")
    header_count = int(chosen.get("header_row_count") or (1 if header_mode == "present" else 0))
    body = parsed[skip:]
    file_columns = _writer_file_columns(writer)
    if header_mode == "present":
        if len(body) < header_count:
            return {"reason": "ragged_row"}
        header_rows = body[:header_count]
        data_rows = body[header_count:]
        width = len(header_rows[0]["fields"])
        if any(len(row["fields"]) != width for row in header_rows):
            return {"reason": "ragged_row"}
        titles = []
        for position in range(width):
            titles.append([row["fields"][position] for row in header_rows])
        assignments = {}
        for position, position_titles in enumerate(titles):
            hits = [col for col in file_columns if _header_matches(position_titles, col)]
            if len(hits) > 1:
                return {"reason": "schema_resolution"}
            if len(hits) == 1:
                assignments[position] = hits[0]
        # Two headers that land on one column would let the later cell
        # silently overwrite the earlier one. Refuse the file instead.
        claimed = {}
        for col in assignments.values():
            claimed[col.name] = claimed.get(col.name, 0) + 1
        duplicates = sorted(name for name, count in claimed.items() if count > 1)
        headers = []
        for position, position_titles in enumerate(titles):
            column_name = assignments[position].name if position in assignments else None
            headers.append({"column": column_name, "titles": position_titles})
    else:
        duplicates = []
        header_rows = []
        data_rows = body
        width = len(file_columns)
        assignments = {index: col for index, col in enumerate(file_columns)}
        headers = [
            {"column": col.name, "titles": ["column_{0}".format(index + 1)]}
            for index, col in enumerate(file_columns)
        ]
    numbered = []
    for index, record in enumerate(data_rows, start=1):
        numbered.append({**record, "row_number": index})
    return {
        "reason": None,
        "width": width,
        "duplicates": duplicates,
        "assignments": assignments,
        "headers": headers,
        "data_rows": numbered,
        "header_rows": header_rows,
    }


def _missing_required(writer, assignments: dict) -> list:
    assigned = {col.name for col in assignments.values()}
    missing = []
    for col in _writer_file_columns(writer):
        if _must_emit(col) and col.name not in assigned:
            missing.append(col.name)
    return missing


def _build_row(record: dict, table: dict, reader, operators: list) -> dict:
    cells = {}
    unprojected = {}
    for position, writer_col in table["assignments"].items():
        if position >= len(record["fields"]):
            continue
        raw = record["fields"][position]
        kind = record["kinds"][position]
        parsed = parse_cell(raw, writer_col)
        target = _project_target(writer_col, reader, operators)
        if target is None:
            unprojected[writer_col.name] = raw
            continue
        errors = list(parsed["errors"])
        value = parsed["value"]
        precision = False
        source_integer = None
        if "type_error" not in errors and writer_col.datatype != target.datatype:
            promoted = promote(value, writer_col.datatype, target.datatype)
            value = promoted["value"]
            precision = promoted["precision_loss"]
            source_integer = promoted.get("source_integer")
        if target.required and value is None and "required_null" not in errors:
            errors.append("required_null")
        if not target.required:
            errors = [item for item in errors if item != "required_null"]
        cell = {
            "string_value": raw,
            "value": value,
            "errors": errors,
            "syntactic_empty": syntactic_empty(kind, raw),
        }
        if writer_col.datatype == "string" or target.datatype == "string":
            report = length_report(raw if raw is not None else "")
            cell["nfc"] = report["nfc"]
            if report["lengths_differ"]:
                cell["raw_length"] = report["raw_length"]
                cell["nfkc_length"] = report["nfkc_length"]
            if report["compatibility_fold"]:
                cell["compatibility_fold"] = True
        if writer_col.datatype != target.datatype and "type_error" not in errors:
            cell["promoted_from"] = writer_col.datatype
        if precision:
            cell["precision_loss"] = True
            cell["source_integer"] = source_integer
        cells[target.name] = cell
    for reader_col in reader.columns:
        if reader_col.name in cells:
            continue
        if reader_col.virtual:
            cells[reader_col.name] = {
                "string_value": None,
                "value": reader_col.default,
                "errors": [],
                "virtual": True,
            }
            continue
        if reader_col.publication != "public":
            continue
        if reader_col.has_default:
            cell_errors = ["required_null"] if reader_col.required and reader_col.default is None else []
            cells[reader_col.name] = {
                "string_value": None,
                "value": reader_col.default,
                "errors": cell_errors,
                "omitted": True,
            }
        else:
            cells[reader_col.name] = {
                "string_value": None,
                "value": None,
                "errors": ["required_null"] if reader_col.required else [],
                "omitted": True,
            }
    flags = []
    if any(cell.get("compatibility_fold") for cell in cells.values()):
        flags.append("compatibility_fold")
    return {
        "source_row": record["source_row"],
        "row_number": record["row_number"],
        "cells": cells,
        "flags": flags,
        "unprojected": unprojected,
    }


def _apply_store(registry: Registry, rows: list, links: list) -> None:
    by_number = {row["row_number"]: row for row in rows}
    for link in links:
        row = by_number.get(link["row_number"])
        if row is None:
            continue
        if link["method"] != "new":
            continue
        if link["canonical_id"] in registry.store.rows:
            continue
        stored = {"canonical_id": link["canonical_id"]}
        for name, cell in row["cells"].items():
            if cell.get("virtual"):
                continue
            stored[name] = _stored_text(cell)
        registry.store.upsert(stored)


def _parts_columns(organization_name: str, aliases: tuple, include_bin: bool = False, bin_state: str = "absent"):
    columns = [
        column("supplier_id", required=True, null_tokens=("NA",)),
        column("sku", required=False, null_tokens=("NA",)),
        column(organization_name, aliases=aliases, required=True, null_tokens=("NA",)),
        column("brand", null_tokens=("NA",)),
        column("size", null_tokens=("NA",)),
        column("title", null_tokens=("NA",)),
        column("pack_qty", datatype="integer", required=False, null_tokens=("NA",)),
    ]
    if include_bin:
        columns.append(
            column("bin_code", datatype="string", required=False, publication=bin_state, null_tokens=("NA",))
        )
    return columns


def run_demo(out_dir, dry_run: bool = False, sleeper=None, attempts: int = 3) -> dict:
    examples = PROJECT_ROOT / "examples"
    registry = Registry()
    seed = json.loads((examples / "canonical_seed.json").read_text(encoding="utf-8"))
    for row in seed:
        registry.store.upsert(row)
    v1 = make_schema(1, _parts_columns("org_name", ()), dialect=make_dialect())
    registered = registry.register(v1, [])
    if not registered["ok"]:
        raise RuntimeError(registered["reasons"])
    v2_columns = _parts_columns("organization", ("org_name",))
    v2 = make_schema(2, v2_columns, dialect=make_dialect())
    registered = registry.register(
        v2,
        [{"op": "rename_column", "from": "org_name", "to": "organization"}],
    )
    if not registered["ok"]:
        raise RuntimeError(registered["reasons"])
    vendor_v1 = read_with_retry(examples / "vendor_v1.csv", attempts=attempts, sleeper=sleeper)
    vendor_v2 = read_with_retry(examples / "vendor_v2.csv", attempts=attempts, sleeper=sleeper)
    first = ingest_bytes(registry, vendor_v1, writer_version=1, dry_run=dry_run)
    second = ingest_bytes(registry, vendor_v2, writer_version=2, dry_run=dry_run)
    v3 = make_schema(
        3,
        _parts_columns("organization", ("org_name",), include_bin=True, bin_state="delete_only"),
        dialect=make_dialect(),
    )
    registered = registry.register(v3, [{"op": "add_column", "name": "bin_code"}])
    if not registered["ok"]:
        raise RuntimeError(registered["reasons"])
    stale = ingest_bytes(registry, vendor_v1, writer_version=1, dry_run=dry_run)
    messy = (examples / "messy_semicolon.csv").read_text(encoding="utf-8")
    probe = detect_dialect(messy)
    batches = {"vendor_v1": first, "vendor_v2": second, "stale_v1": stale}
    manifest = {
        "unicode_version": unidata_version(),
        "reader_version": registry.latest().number,
        "versions": [version.number for version in registry.versions],
        "rewrite_organization": registry.rewrite("organization"),
        "batches": [
            {
                "name": name,
                "status": batch["status"],
                "reason": batch["reason"],
                "rows": len(batch["rows"]),
                "quarantined_rows": len(batch["quarantined_rows"]),
                "links": [
                    {"row_number": item["row_number"], "canonical_id": item["canonical_id"], "method": item["method"]}
                    for item in batch["links"]
                ],
            }
            for name, batch in batches.items()
        ],
        "dialect_probe": {
            "status": probe["status"],
            "source": probe["source"],
            "delimiter": None if probe["dialect"] is None else probe["dialect"]["delimiter"],
            "quote": None if probe["dialect"] is None else probe["dialect"]["quote"],
            "sniffer_delimiter": None if probe["sniffer"] is None else probe["sniffer"]["delimiter"],
        },
        "input_sha256": {
            "vendor_v1": hashlib.sha256(vendor_v1).hexdigest(),
            "vendor_v2": hashlib.sha256(vendor_v2).hexdigest(),
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    if not dry_run:
        destination = Path(out_dir)
        destination.mkdir(parents=True, exist_ok=True)
        _write_json(destination / "manifest.json", manifest)
        _write_json(
            destination / "registry.json",
            {
                "versions": [version.to_plain() for version in registry.versions],
                "operators": registry.operators,
                "store": registry.store.sorted_rows(),
            },
        )
        for name, batch in batches.items():
            _write_json(destination / "{0}.json".format(name.replace("_", "-")), batch)
        _write_json(destination / "dialect-probe.json", probe_plain(probe))
    return {"registry": registry, "manifest": manifest, "batches": batches, "probe": probe}


def probe_plain(probe: dict) -> dict:
    return {
        "status": probe["status"],
        "source": probe["source"],
        "dialect": probe["dialect"],
        "sniffer": probe["sniffer"],
        "candidates": probe["candidates"],
        "tie_broken": probe["tie_broken"],
    }


def _write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")
