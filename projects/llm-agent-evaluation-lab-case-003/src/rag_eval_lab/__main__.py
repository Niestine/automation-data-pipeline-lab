"""CLI for the offline RAG evaluation lab."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from .corpus import is_fact_single_heavy, load_corpus, select_scored
from .errors import LabError, LabInputError
from .generator import FakeGenerator
from .hybrid import best_text_weights
from .leaderboard import assert_round_trip
from .ledger import prompt_hash
from .pipeline import RagLab, RunConfig, summarize
from .retrieve import LexicalRetriever
from .slices import run_counterfactual, run_noise_curve
from .sweep import load_spec, run_metric_sweep
from .telemetry import JsonLogger, ManualClock, RecordingSleeper

EXIT_INPUT = 2


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LabInputError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LabInputError(f"malformed JSON: {path}") from exc


def load_generator_script(path: Path) -> tuple[str, str, dict[str, list[dict[str, Any]]]]:
    payload = load_json(path)
    if not isinstance(payload, dict):
        raise LabInputError("generator script must be an object")
    provider_id = payload.get("provider_id")
    prompt = payload.get("prompt")
    items = payload.get("items")
    if not isinstance(provider_id, str) or not isinstance(prompt, str) or not isinstance(items, dict):
        raise LabInputError("generator script needs provider_id, prompt, and items")
    script: dict[str, list[dict[str, Any]]] = {}
    for item_id, steps in items.items():
        if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
            raise LabInputError(f"script entry {item_id} must be a list of objects")
        script[item_id] = steps
    return provider_id, prompt, script


def build_report(examples: Path, *, approve: bool, dry_run: bool) -> dict[str, Any]:
    corpus = load_corpus(examples / "corpus.json")
    provider_id, prompt, script = load_generator_script(examples / "generator_script.json")
    missing = [item_id for item_id in corpus.regression_item_ids if item_id not in script]
    if "q-schema-retry" not in script:
        missing.append("q-schema-retry")
    if missing:
        raise LabInputError("generator script missing items: " + ", ".join(missing))
    one_shot = [item for item in corpus.ordered_items() if item.batch == "one_shot"]
    if not is_fact_single_heavy(one_shot):
        raise LabInputError("one-shot control is not fact_single-heavy")
    if any(item.item_id in {row.item_id for row in select_scored(corpus.ordered_items())} for item in one_shot):
        raise LabInputError("one-shot control leaked into the scored set")
    clock = ManualClock()
    lab = RagLab(
        corpus,
        FakeGenerator(script, provider_id),
        prompt,
        retriever=LexicalRetriever(corpus),
        logger=JsonLogger(),
        clock=clock,
        sleeper=RecordingSleeper(clock),
    )
    config = RunConfig(approve=approve, dry_run=dry_run)
    demo_ids = list(corpus.regression_item_ids) + ["q-schema-retry"]
    results = lab.run_ids(demo_ids, config)
    regression = [result for result in results if result["item_id"] in corpus.regression_item_ids]
    lab.commit(regression, config)
    report = summarize(corpus, results, dry_run=dry_run)
    report["provider_id"] = provider_id
    report["prompt_hash"] = prompt_hash(prompt)
    report["ledger_rows"] = len(lab.ledger.rows)
    report["retriever_id"] = LexicalRetriever.retriever_id
    report["round_trip_filter"] = False
    report["round_trip_retriever_id"] = None
    assert_round_trip(LexicalRetriever.retriever_id, None)
    poison_chunks = [chunk for chunk in corpus.chunks.values() if "poison" in chunk.tags and chunk.indexed]
    report["poison_chunk_count"] = len(poison_chunks)
    noise_script = load_json(examples / "noise_script.json")
    report["noise_curve"] = run_noise_curve(corpus, corpus.items["q-lamp"], noise_script["ratios"])
    cf_script = load_json(examples / "counterfactual_script.json")
    report["counterfactual"] = run_counterfactual(corpus, corpus.items["q-radio-cf"], cf_script["adopt"])
    hybrid = load_json(examples / "hybrid_fixture.json")
    report["hybrid"] = best_text_weights(hybrid["examples"], hybrid["weights"], hybrid["floor"])
    sweep = run_metric_sweep(load_spec(examples / "sweep_tokens.json"))
    report["retriever_leaderboard"] = {
        "decisive": sweep["leaderboard"]["decisive"],
        "sort_keys": sweep["leaderboard"]["sort_keys"],
        "top_config": sweep["leaderboard"]["rows"][0]["config_id"],
        "operating_point": sweep["operating_point"]["config_id"],
        "overlap_tuned": False,
    }
    report["generator_comparison"] = sweep["generator_comparison"]
    report["sweep_k1_f1"] = sweep["by_id"]["k1-s1-o0-f0.0"]["f1"]
    report["sweep_k4_f1"] = sweep["by_id"]["k4-s1-o0-f0.0"]["f1"]
    report["events"] = len(lab.logger.events)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline RAG evaluation lab with planted claims and citation guardrails.")
    parser.add_argument("--examples", type=Path, default=None, help="Directory of synthetic fixtures.")
    parser.add_argument("--approve", action="store_true", help="Release require_approval items. Rejects stay rejected.")
    parser.add_argument("--dry-run", action="store_true", help="Score in memory and skip the confabulation ledger.")
    return parser


def main(argv: list[str] | None = None, stdout: TextIO | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = stdout or sys.stdout
    examples = args.examples or (project_root() / "examples")
    try:
        report = build_report(examples, approve=args.approve, dry_run=args.dry_run)
    except LabInputError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INPUT
    except LabError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(report, out, indent=2, sort_keys=True)
    out.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
