"""Command line for the two-horizon fixture crawl."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from incremental_crawl_lab.clock import ManualClock
from incremental_crawl_lab.config import load_config
from incremental_crawl_lab.crawl import Crawler
from incremental_crawl_lab.errors import ConfigError, LabError, SimulatedCrash
from incremental_crawl_lab.fixture import load_site
from incremental_crawl_lab.report import build_report, collection_lines
from incremental_crawl_lab.store import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incremental_crawl_lab")
    parser.add_argument("--config", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-jsonl", default=None)
    parser.add_argument("--weeks", type=int, default=2)
    parser.add_argument("--crash-at", choices=("pre_request", "post_result"), default=None)
    return parser


def execute(argv: list[str] | None = None) -> tuple[int, dict]:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse has already written usage or help text to the terminal.
        code = 0 if exc.code is None else int(exc.code)
        return code, ({} if code == 0 else {"error": "invalid arguments"})
    if args.weeks < 1:
        return 2, {"error": "weeks must be >= 1"}
    if not args.dry_run and not args.state_dir:
        return 2, {"error": "state-dir is required unless --dry-run is set"}
    try:
        config = load_config(args.config)
        origin = load_site(args.site)
    except (OSError, json.JSONDecodeError, ConfigError, KeyError, TypeError, ValueError) as exc:
        return 2, {"error": str(exc)}
    state_path: Path | None = None
    store: Store | None = None
    try:
        if args.dry_run:
            store = Store.open(None, durable=False)
            state_dir = None
        else:
            state_path = Path(args.state_dir)
            state_path.mkdir(parents=True, exist_ok=True)
            store = Store.open(str(state_path / "crawl.sqlite"), durable=True)
            state_dir = str(state_path)
        clock = ManualClock()
        crawler = Crawler(config, store, origin, clock)
        crawler.crash_point = args.crash_at
        start = clock.now
        for week in range(args.weeks):
            if week:
                target = start + week * float(config.horizon_seconds)
                if clock.now < target:
                    clock.sleep(target - clock.now)
                origin.apply_horizon(week, clock.now)
            crawler.run()
        window = None
        if crawler.first_horizon_start is not None and crawler.last_horizon_end is not None:
            window = (crawler.first_horizon_start, crawler.last_horizon_end)
        report = build_report(
            store,
            origin,
            config,
            dry_run=bool(args.dry_run),
            state_dir=None if args.dry_run else state_dir,
            requests=crawler.transmissions,
            window=window,
        )
        if state_path is not None:
            (state_path / "report.json").write_text(
                json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            lines = collection_lines(store)
            (state_path / "collection.jsonl").write_text(
                "\n".join(lines) + ("\n" if lines else ""),
                encoding="utf-8",
            )
            if args.log_jsonl:
                _write_events(store, Path(args.log_jsonl))
        elif args.log_jsonl:
            _write_events(store, Path(args.log_jsonl))
        return 0, report
    except SimulatedCrash as exc:
        return 3, {"error": str(exc), "crash": exc.args[0] if exc.args else "crash"}
    except LabError as exc:
        return 2, {"error": str(exc)}
    finally:
        if store is not None:
            store.close()


def _write_events(store: Store, path: Path) -> None:
    lines = []
    for event in store.events():
        lines.append(
            json.dumps(
                {
                    "id": event["id"],
                    "url": event["url"],
                    "ts": event["ts"],
                    "kind": event["kind"],
                    "detail": event["detail"],
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    code, payload = execute(argv)
    if payload:
        json.dump(payload, sys.stdout, ensure_ascii=True, sort_keys=True)
        sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
