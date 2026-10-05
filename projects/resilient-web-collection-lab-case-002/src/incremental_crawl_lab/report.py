"""Summarize a finished run from the frontier and the fixture change log."""

from __future__ import annotations

import json

from incremental_crawl_lab.config import Config
from incremental_crawl_lab.fixture import FixtureOrigin
from incremental_crawl_lab.metrics import collection_mean, time_average
from incremental_crawl_lab.store import Store


def build_report(
    store: Store,
    origin: FixtureOrigin,
    config: Config,
    *,
    dry_run: bool,
    state_dir: str | None,
    requests: int,
    window: tuple[float, float] | None,
) -> dict:
    events = store.events()
    counts = {
        "not_modified_304": 0,
        "material_change_count": 0,
        "cosmetic_change_count": 0,
        "byte_change_count": 0,
        "retries": 0,
        "possible_soft_errors": 0,
    }
    blocked: set[str] = set()
    syncs: dict[str, list[float]] = {}
    for event in events:
        kind = event["kind"]
        detail = json.loads(event["detail"])
        if kind == "validator_not_modified":
            counts["not_modified_304"] += 1
        elif kind == "material_change":
            counts["material_change_count"] += 1
        elif kind == "cosmetic_change":
            counts["cosmetic_change_count"] += 1
        elif kind == "retry":
            counts["retries"] += 1
        elif kind == "soft_error":
            counts["possible_soft_errors"] += 1
        elif kind == "robots_block" and event["url"]:
            blocked.add(event["url"])
        if detail.get("byte_delta"):
            counts["byte_change_count"] += int(detail["byte_delta"])
        if detail.get("oracle_sync") and event["url"]:
            syncs.setdefault(event["url"], []).append(float(event["ts"]))
    changes: dict[str, list[float]] = {}
    for url, ts in origin.changes:
        changes.setdefault(url, []).append(ts)
    freshness_values: list[float] = []
    age_values: list[float] = []
    if window is not None and window[1] > window[0] and syncs:
        for url, stamps in syncs.items():
            freshness, age = time_average(stamps, changes.get(url, []), window[0], window[1])
            freshness_values.append(freshness)
            age_values.append(age)
        source = "fixture_oracle"
    else:
        source = "request_counters"
    state = store.run_state() or {}
    censored = store.conn.execute(
        "SELECT COUNT(*) FROM urls WHERE rate_censored=1"
    ).fetchone()[0]
    return {
        "time_average_freshness": collection_mean(freshness_values),
        "time_average_age": collection_mean(age_values),
        "freshness_source": source,
        "not_modified_304": counts["not_modified_304"],
        "byte_change_count": counts["byte_change_count"],
        "material_change_count": counts["material_change_count"],
        "cosmetic_change_count": counts["cosmetic_change_count"],
        "robots_blocks": len(blocked),
        "retries": counts["retries"],
        "page_budget_left": int(state.get("page_budget_left", 0)),
        "retry_budget_left": int(state.get("retry_budget_left", 0)),
        "possible_soft_errors": counts["possible_soft_errors"],
        "rate_censored_urls": int(censored),
        "simhash_k": config.simhash_k,
        "material_detection": bool(config.material_detection and config.simhash_k is not None),
        "requests": requests,
        "dry_run": dry_run,
        "state_dir": state_dir,
    }


def collection_lines(store: Store) -> list[str]:
    rows = store.list_status("live")
    lines = []
    for row in rows:
        payload = {
            "url": row["url"],
            "depth": row["depth"],
            "sha256": row["sha256"],
            "simhash": row["simhash"],
            "charset": row["charset"],
            "etag": row["etag"],
            "weak": bool(row["weak"]),
            "material_change_count": row["material_change_count"],
            "cosmetic_change_count": row["cosmetic_change_count"],
            "byte_change_count": row["byte_change_count"],
            "lambda_hat": row["lambda_hat"],
            "rate_censored": bool(row["rate_censored"]),
            "synced_at": row["synced_at"],
            "status": row["status"],
        }
        lines.append(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    return lines
