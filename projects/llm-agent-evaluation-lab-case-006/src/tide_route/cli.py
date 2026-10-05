"""Print the tide-desk routing table and the cost-quality summary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tide_route.evaluate import benchmark
from tide_route.fit import prune_agreeing
from tide_route.load import read_json


def project_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def run(examples: Path, out: Path | None, dry_run: bool) -> int:
    try:
        validation = read_json(examples / "validation_split.json")
        fixture = read_json(examples / "eval_fixture.json")
        # A well-formed JSON file with missing or mistyped fields is still a
        # fixture error, not a traceback.
        kept = prune_agreeing(validation["answers"], validation["costs"])
        report = benchmark(fixture)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"fixture_error {exc.__class__.__name__}", file=sys.stderr)
        return 2
    lines = [
        "tide-desk case-006",
        "validation_kept " + ",".join(kept),
        f"dry_run {str(dry_run).lower()}",
    ]
    for row in report["routes"]:
        lines.append(
            f"{row['query_id']} {row['model_id']} {row['cost']:.6f} "
            f"{'ok' if row['ok'] else 'error'} calls={row['http_calls']}"
        )
    ladder_cost, ladder_quality = report["ladder_point"]
    lines.append(
        f"ladder_mean_cost {ladder_cost:.6f} ladder_mean_quality {ladder_quality:.6f}"
    )
    lines.append(f"oracle_quality {report['oracle_quality']:.6f}")
    lines.append(f"product_aiq {report['product_aiq']:.6f}")
    lines.append(f"zero_aiq {report['zero_aiq']:.6f}")
    crossover = report["crossover_epsilon"]
    lines.append(
        "crossover_epsilon " + ("none" if crossover is None else f"{crossover:.1f}")
    )
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if out is not None and not dry_run:
        payload = {
            "validation_kept": kept,
            "routes": report["routes"],
            "product_aiq": report["product_aiq"],
            "zero_aiq": report["zero_aiq"],
            "oracle_quality": report["oracle_quality"],
            "ladder_point": list(report["ladder_point"]),
            "crossover_epsilon": crossover,
            "dry_run": False,
        }
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the tide-desk fallback router")
    parser.add_argument("--examples", type=Path, default=project_dir() / "examples")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return run(args.examples, args.out, args.dry_run)
