"""Load the bundled curb-permit catalog and print the release report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from curbgate.models import GoldenCase, RunManifest, load_json
from curbgate.runner import run_comparison
from curbgate.scoring import expand_negation


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_catalog(root: Path | None = None) -> dict[str, Any]:
    base = root or project_root()
    examples = base / "examples"
    baseline = RunManifest.from_dict(load_json(str(examples / "manifest_baseline.json")))
    candidate = RunManifest.from_dict(load_json(str(examples / "manifest_candidate.json")))
    ledger = load_json(str(examples / "ledger.json"))
    catalog = load_json(str(examples / "catalog.json"))
    lexicon = load_json(str(examples / "lexicon.json"))
    cases = [GoldenCase.from_dict(row) for row in catalog["cases"]]
    scripts: dict[str, Any] = {row["id"]: row["outputs"] for row in catalog["cases"]}
    for row in expand_negation(lexicon):
        raw = {
            "id": row["id"],
            "cluster_id": row["id"],
            "template_id": row["template_id"],
            "suite": "core",
            "prompt_name": baseline.prompt_name,
            "schema_id": "curb.label_v1",
            "schema_key_order": ["answer"],
            "risk_tags": row["risk_tags"],
            "test_type": "mft",
            "capability": row["capability"],
            "suite_family": "classification",
            "format_level": "constrained",
            "expectation": {"label": row["label"], "user_text": row["text"]},
        }
        cases.append(GoldenCase.from_dict(raw))
        answer = row["label"]
        body = {"raw": json.dumps({"answer": answer}), "response_model": baseline.response_model}
        scripts[row["id"]] = {"baseline": [body], "candidate": [dict(body)]}
    return {
        "cases": cases,
        "scripts": scripts,
        "baseline": baseline,
        "candidate": candidate,
        "ledger": ledger,
    }


def build_report(root: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    loaded = load_catalog(root)
    return run_comparison(
        loaded["cases"],
        loaded["scripts"],
        loaded["baseline"],
        loaded["candidate"],
        loaded["ledger"],
        dry_run=dry_run,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_lab.py", description="Compare the bundled baseline and candidate prompt runs.")
    parser.add_argument("--dry-run", action="store_true", help="screen tool writes but do not apply them")
    args = parser.parse_args(argv)
    report = build_report(dry_run=args.dry_run)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0
