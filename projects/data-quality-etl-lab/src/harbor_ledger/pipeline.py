"""Seal a supplier drop into a canonical CSV and a JCS anomaly report.

Detectors run as pure functions of one typed snapshot. Survivor removal happens
only after that union is sealed. Re-running the same bytes, seed, and schema
reproduces the clean CSV and the report digest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

from .csvio import parse_csv, write_csv
from .decode import FatalDecodeError, decode_owned, decode_supplier, output_encoding
from .dedup import match_rows
from .detectors import DETECTORS, run_detectors
from .errors import DecodeError, SchemaError
from .jcs import assert_ascii_keys, canonicalize
from .metrics import score_slices
from .models import ViewRow
from .retry import read_with_retry
from .schema import Schema, cast_table, render_value

logger = logging.getLogger("harbor_ledger")


@dataclass
class RunResult:
    status: str
    clean_csv: bytes | None = None
    report: dict | None = None
    report_jcs: bytes | None = None
    digest: str | None = None
    manifest: dict | None = None
    log: list[str] = field(default_factory=list)
    error: str | None = None
    findings: list[dict] = field(default_factory=list)


def load_profile(path: str | Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SchemaError("profile must be an object")
    for key in ("seed", "replacement_threshold", "detector_order", "outlier", "dedup", "retry"):
        if key not in payload:
            raise SchemaError(f"profile is missing {key}")
    payload.setdefault("repair_between_detectors", False)
    _validate_profile(payload)
    return payload


def _validate_profile(profile: dict) -> None:
    """Reject a profile the sealed run would otherwise fail on halfway through."""

    order = profile["detector_order"]
    if not isinstance(order, list) or not order or any(name not in DETECTORS for name in order):
        raise SchemaError(f"detector_order must list known detectors: {', '.join(DETECTORS)}")
    for label, value in (
        ("seed", profile["seed"]),
        ("replacement_threshold", profile["replacement_threshold"]),
        ("outlier.minimum_rows", profile["outlier"].get("minimum_rows", 5)),
        ("dedup.window", profile["dedup"].get("window")),
        ("dedup.q", profile["dedup"].get("q")),
        ("dedup.edit_agree", profile["dedup"].get("edit_agree")),
        ("retry.attempts", profile["retry"].get("attempts")),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SchemaError(f"profile {label} must be a non-negative integer")
    if profile["retry"]["attempts"] < 1 or profile["dedup"]["q"] < 1:
        raise SchemaError("profile retry.attempts and dedup.q must be positive")
    delays = profile["retry"].get("delays_ms")
    if not isinstance(delays, list) or not all(isinstance(item, int) and not isinstance(item, bool) for item in delays):
        raise SchemaError("profile retry.delays_ms must be a list of integers")
    dedup = profile["dedup"]
    decimals = [("outlier.threshold", profile["outlier"].get("threshold"))]
    decimals += [
        (f"dedup.{key}", dedup.get(key))
        for key in ("jaro_agree", "cosine_agree", "monge_agree", "link_threshold", "possible_threshold")
    ]
    fields = dedup.get("fields")
    keys = dedup.get("blocking_keys")
    if not isinstance(fields, list) or not fields or not isinstance(keys, list) or not keys:
        raise SchemaError("profile dedup.fields and dedup.blocking_keys must be non-empty lists")
    for item in fields:
        if not isinstance(item, dict) or item.get("kind") not in {"short", "token"} or not isinstance(item.get("name"), str):
            raise SchemaError("each dedup field needs a name and kind short or token")
        decimals += [(f"dedup {item['name']}.m", item.get("m")), (f"dedup {item['name']}.u", item.get("u"))]
    for label, value in decimals:
        try:
            parsed = Decimal(value) if isinstance(value, str) else None
        except InvalidOperation:
            parsed = None
        if parsed is None or not parsed.is_finite():
            raise SchemaError(f"profile {label} must be a decimal string")
    for item in fields:
        m, u = Decimal(item["m"]), Decimal(item["u"])
        if not (0 < m < 1 and 0 < u < 1):
            raise SchemaError(f"dedup {item['name']} m and u must lie strictly between 0 and 1")


def run_path(
    path: str | Path,
    schema: Schema,
    profile: dict,
    *,
    out_dir: str | Path | None = None,
    dry_run: bool = False,
    owned: bool = False,
    reader=None,
    sleeper=None,
    oracle: list[dict] | None = None,
    slices: dict[str, str] | None = None,
    injector_shortfall: int | None = None,
) -> RunResult:
    retry = profile["retry"]
    data = read_with_retry(
        path,
        attempts=int(retry["attempts"]),
        delays_ms=list(retry["delays_ms"]),
        reader=reader,
        sleeper=sleeper,
    )
    result = run_bytes(
        data,
        schema,
        profile,
        owned=owned,
        oracle=oracle,
        slices=slices,
        injector_shortfall=injector_shortfall,
    )
    if dry_run or out_dir is None or result.status == "fatal":
        return result
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    if result.status == "ok" and result.clean_csv is not None:
        (destination / "clean.csv").write_bytes(result.clean_csv)
    if result.report_jcs is not None and result.report is not None and result.manifest is not None:
        (destination / "report.jcs").write_bytes(result.report_jcs)
        (destination / "report.json").write_text(
            json.dumps(result.report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (destination / "run_manifest.json").write_text(
            json.dumps(result.manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return result


def run_bytes(
    data: bytes,
    schema: Schema,
    profile: dict,
    *,
    owned: bool = False,
    oracle: list[dict] | None = None,
    slices: dict[str, str] | None = None,
    injector_shortfall: int | None = None,
) -> RunResult:
    log: list[str] = []

    def note(message: str) -> None:
        log.append(message)
        logger.info(message)

    declared = schema.dialect.encoding
    note(f"output_encoding {output_encoding(declared)}")
    if owned:
        try:
            text = decode_owned(data)
        except FatalDecodeError as exc:
            note("fatal decode")
            return RunResult(status="fatal", log=log, error=str(exc))
        encoding_used = "utf-8"
        bom = False
        bom_override = False
        replacement_count = 0
        quarantined = False
    else:
        try:
            decoded = decode_supplier(data, declared, int(profile["replacement_threshold"]))
        except DecodeError as exc:
            note("fatal decode")
            return RunResult(status="fatal", log=log, error=str(exc))
        text = decoded.text
        encoding_used = decoded.encoding_used
        bom = decoded.bom
        bom_override = decoded.bom_override
        replacement_count = decoded.replacement_count
        quarantined = decoded.quarantined
    note(
        f"decode encoding={encoding_used} bom={bom} bom_override={bom_override} "
        f"replacements={replacement_count}"
    )
    if quarantined:
        note("quarantine")
        report = _empty_report(profile, replacement_count, "quarantined")
        report["quarantine"] = {
            "reason": "replacement count exceeded the manifest threshold",
            "replacement_count": replacement_count,
        }
        return _finish(
            status="quarantined",
            report=report,
            schema=schema,
            profile=profile,
            data=data,
            encoding_used=encoding_used,
            bom_override=bom_override,
            replacement_count=replacement_count,
            injector_shortfall=injector_shortfall,
            log=log,
            findings=[],
        )

    parsed = parse_csv(text, schema.dialect, len(schema.fields))
    names = [spec.name for spec in schema.fields]
    if schema.dialect.header:
        if parsed.header is None:
            note("fatal header")
            return RunResult(status="fatal", log=log, error="header row is missing")
        if len(parsed.header) != len(names):
            note("fatal header")
            return RunResult(status="fatal", log=log, error="header row is ragged")
    rows = cast_table(parsed.records, schema)
    annotations = _annotations(rows, schema, parsed.header if schema.dialect.header else None, names)
    note(f"parsed data_rows={len(rows)} physical={len(parsed.physical)}")
    if profile.get("repair_between_detectors"):
        note("repair-between path")
    else:
        note("detector union sealed")
    findings = run_detectors(
        rows,
        schema,
        profile,
        order=list(profile["detector_order"]),
        repair_between=bool(profile.get("repair_between_detectors")),
    )
    findings = sorted(
        findings,
        key=lambda item: (item["source_row"], item["column"], item["detector"], item["class"], item["rule"]),
    )
    comparable = [row for row in rows if row.row_id and not row.ragged and not row.cast_errors]
    links, review, clusters = match_rows(comparable, profile)
    kept = _publishable_rows(rows, schema, clusters)
    clean_rows = [
        {spec.name: render_value(row.values.get(spec.name)) for spec in schema.fields}
        for row in kept
    ]
    metrics = None
    if oracle is not None:
        if slices is None:
            # Every parsed row is in a slice, so a finding on a row the oracle
            # calls clean still counts as a false positive.
            slices = {row.row_id: "eval" for row in rows if row.row_id}
            slices.update({cell["row_id"]: cell.get("slice", "eval") for cell in oracle})
        metrics = score_slices(findings, oracle, slices)
    report = {
        "annotations": annotations,
        "clean_count": len(clean_rows),
        "clean_rows": clean_rows,
        "clusters": clusters,
        "detector_order": list(profile["detector_order"]),
        "findings": findings,
        "links": [_pair(item) for item in links],
        "metrics": metrics,
        "quarantine": None,
        "replacement_count": replacement_count,
        "review": [_pair(item) for item in review],
        "status": "ok",
        "unicode_version": unicodedata.unidata_version,
    }
    clean_csv = write_csv(names, [[row[name] for name in names] for row in clean_rows])
    note(f"emit clean_rows={len(clean_rows)} findings={len(findings)}")
    return _finish(
        status="ok",
        report=report,
        schema=schema,
        profile=profile,
        data=data,
        encoding_used=encoding_used,
        bom_override=bom_override,
        replacement_count=replacement_count,
        injector_shortfall=injector_shortfall,
        log=log,
        findings=findings,
        clean_csv=clean_csv,
    )


def _publishable_rows(rows: list[ViewRow], schema: Schema, clusters: list[list[str]]) -> list[ViewRow]:
    drop_sources = _cluster_drops(rows, clusters, schema)
    drop_sources.update(_duplicate_key_drops(rows, schema))
    kept = []
    for row in rows:
        if row.source_row in drop_sources or not _structurally_ok(row, schema):
            continue
        kept.append(row)
    kept.sort(
        key=lambda row: (
            tuple(render_value(row.values.get(name)) for name in schema.primary_key),
            row.source_row,
        )
    )
    return kept


def _cluster_drops(rows: list[ViewRow], clusters: list[list[str]], schema: Schema) -> set[int]:
    grouped: dict[str, list[ViewRow]] = defaultdict(list)
    for row in rows:
        if row.row_id and _structurally_ok(row, schema):
            grouped[row.row_id].append(row)
    drops: set[int] = set()
    for cluster in clusters:
        members: list[ViewRow] = []
        for row_id in cluster:
            members.extend(grouped.get(row_id, []))
        if len(members) < 2:
            continue
        members.sort(key=lambda row: (row.source_row, row.row_id))
        for row in members[1:]:
            drops.add(row.source_row)
    return drops


def _structurally_ok(row: ViewRow, schema: Schema) -> bool:
    if row.ragged or row.parse_errors or not row.row_id:
        return False
    if any(row.cast_errors.get(spec.name) for spec in schema.fields):
        return False
    if any(schema.requires(spec) and row.values.get(spec.name) is None for spec in schema.fields):
        return False
    return True


def _duplicate_key_drops(rows: list[ViewRow], schema: Schema) -> set[int]:
    grouped: dict[str, list[ViewRow]] = defaultdict(list)
    for row in rows:
        if row.row_id and _structurally_ok(row, schema):
            grouped[row.row_id].append(row)
    drops: set[int] = set()
    for group in grouped.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda row: row.source_row)
        for row in group[1:]:
            drops.add(row.source_row)
    return drops


def _annotations(rows: list[ViewRow], schema: Schema, header: list[str] | None, names: list[str]) -> list[dict]:
    annotations: list[dict] = []
    if header is not None and header != names:
        annotations.append(
            {
                "column": "",
                "kind": "header_mismatch",
                "source_row": 0,
                "text": "header names differ from the schema; values were read by position",
            }
        )
    for row in rows:
        for spec in schema.fields:
            for note in row.notes.get(spec.name, []):
                kind = "stripped" if note.startswith("stripped ") else "trim"
                text = note[len("stripped ") :] if kind == "stripped" else note
                annotations.append(
                    {
                        "column": spec.name,
                        "kind": kind,
                        "source_row": row.source_row,
                        "text": text,
                    }
                )
            value = row.values.get(spec.name)
            if spec.identifier_fold and isinstance(value, str):
                folded = unicodedata.normalize("NFKC", value)
                if folded != value:
                    annotations.append(
                        {
                            "after": folded,
                            "before": value,
                            "column": spec.name,
                            "kind": "nfkc",
                            "source_row": row.source_row,
                        }
                    )
    annotations.sort(key=lambda item: (item["source_row"], item["column"], item["kind"], item.get("text", "")))
    return annotations


def _pair(decision) -> dict:
    return {
        "decision": decision.decision,
        "left": decision.left,
        "right": decision.right,
        "skipped": list(decision.skipped),
        "weight": _publish_decimal(decision.weight),
    }


def _publish_decimal(value) -> str:
    return format(Decimal(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN), "f")


def _empty_report(profile: dict, replacement_count: int, status: str) -> dict:
    return {
        "annotations": [],
        "clean_count": 0,
        "clean_rows": [],
        "clusters": [],
        "detector_order": list(profile["detector_order"]),
        "findings": [],
        "links": [],
        "metrics": None,
        "quarantine": None,
        "replacement_count": replacement_count,
        "review": [],
        "status": status,
        "unicode_version": unicodedata.unidata_version,
    }


def _finish(
    *,
    status: str,
    report: dict,
    schema: Schema,
    profile: dict,
    data: bytes,
    encoding_used: str,
    bom_override: bool,
    replacement_count: int,
    injector_shortfall: int | None,
    log: list[str],
    findings: list[dict],
    clean_csv: bytes | None = None,
) -> RunResult:
    assert_ascii_keys(report)
    canonical = canonicalize(report).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    manifest = {
        "bom_override": bom_override,
        "detector_order": list(profile["detector_order"]),
        "encoding": encoding_used,
        "injector_shortfall": injector_shortfall,
        "input_sha256": hashlib.sha256(data).hexdigest(),
        "normalization": {
            "identifier_fold": [spec.name for spec in schema.fields if spec.identifier_fold],
            "nfc": [spec.name for spec in schema.fields if spec.type == "string"],
        },
        "repair_between_detectors": bool(profile.get("repair_between_detectors")),
        "replacement_count": replacement_count,
        "report_sha256": digest,
        "schema_name": schema.name,
        "seed": profile["seed"],
        "thresholds": {
            "link": str(profile["dedup"]["link_threshold"]),
            "outlier": str(profile["outlier"]["threshold"]),
            "possible": str(profile["dedup"]["possible_threshold"]),
        },
        "unicode_version": unicodedata.unidata_version,
        "window": int(profile["dedup"]["window"]),
    }
    note_digest = f"digest {digest}"
    log.append(note_digest)
    logger.info(note_digest)
    return RunResult(
        status=status,
        clean_csv=clean_csv,
        report=report,
        report_jcs=canonical,
        digest=digest,
        manifest=manifest,
        log=log,
        findings=findings,
    )
