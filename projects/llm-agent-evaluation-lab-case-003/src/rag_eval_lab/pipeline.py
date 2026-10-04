"""Retrieve, validate, retry, score, and record one regression run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ledger import ConfabulationLedger, prompt_hash
from .models import LABELS, Corpus, Item
from .retrieve import LexicalRetriever
from .retry import RetryPolicy, backoff_seconds
from .scoring import contract_errors, materialize_payload, require_stratified, resolve_claims, unscored_abstain
from .telemetry import JsonLogger, ManualClock, RecordingSleeper

REGRESSION_SET_ID = "harborline-r1"


@dataclass
class RunConfig:
    k: int = 10
    floor: float = 0.0
    approve: bool = False
    dry_run: bool = False


class RagLab:
    def __init__(
        self,
        corpus: Corpus,
        generator: Any,
        prompt: str,
        *,
        retriever: LexicalRetriever | None = None,
        ledger: ConfabulationLedger | None = None,
        logger: JsonLogger | None = None,
        clock: ManualClock | None = None,
        sleeper: RecordingSleeper | None = None,
        policy: RetryPolicy | None = None,
        seed: int = 7,
        regression_set_id: str = REGRESSION_SET_ID,
    ) -> None:
        self.corpus = corpus
        self.generator = generator
        self.prompt = prompt
        self.retriever = retriever or LexicalRetriever(corpus)
        self.ledger = ledger or ConfabulationLedger()
        self.logger = logger or JsonLogger()
        self.clock = clock or ManualClock()
        self.sleeper = sleeper or RecordingSleeper(self.clock)
        self.policy = policy or RetryPolicy()
        self.seed = seed
        self.regression_set_id = regression_set_id
        self.cache: dict[tuple[Any, ...], dict[str, Any]] = {}

    def run_item(self, item: Item, config: RunConfig | None = None) -> dict[str, Any]:
        cfg = config or RunConfig()
        key = (
            item.item_id,
            cfg.k,
            cfg.floor,
            prompt_hash(self.prompt),
            cfg.approve,
            self.generator.provider_id,
        )
        cached = self.cache.get(key)
        if cached is not None:
            self.logger.event("cache_hit", item_id=item.item_id)
            return cached
        hits = self.retriever.retrieve(item.question, cfg.k, cfg.floor)
        self.logger.event(
            "retrieve",
            item_id=item.item_id,
            chunks=[hit.chunk_id for hit in hits],
            k=cfg.k,
        )
        payload = None
        errors: list[str] = []
        attempts = 0
        for attempt in range(self.policy.attempts):
            attempts = attempt + 1
            raw = self.generator.complete(item.item_id, attempt, hits, self.prompt)
            materialized, pre_errors = materialize_payload(raw, self.corpus)
            errors = list(pre_errors) + contract_errors(materialized, self.corpus, item.item_id)
            self.logger.event("generate", item_id=item.item_id, attempt=attempts, ok=not errors)
            if not errors:
                payload = materialized
                break
            if attempt + 1 < self.policy.attempts:
                delay = backoff_seconds(attempt, self.seed, item.item_id, self.policy.base_seconds)
                self.sleeper.sleep(delay)
                self.logger.event("schema_retry", item_id=item.item_id, delay=delay, attempt=attempts)
        if payload is None:
            result = unscored_abstain(item.item_id, attempts)
            result["errors"] = errors
            self.logger.event("abstain", item_id=item.item_id, reason="schema_failure")
        else:
            result = resolve_claims(self.corpus, item, payload, hits)
            result["item_id"] = item.item_id
            result["attempts"] = attempts
            result["abstain_reason"] = None
            result["errors"] = []
        result["construction_label"] = item.construction_label
        result["batch"] = item.batch
        result["provider_id"] = self.generator.provider_id
        if cfg.approve and result["acceptance"] == "require_approval":
            result["acceptance"] = "approved"
        self.cache[key] = result
        return result

    def run_ids(self, item_ids: list[str] | tuple[str, ...], config: RunConfig | None = None) -> list[dict[str, Any]]:
        return [self.run_item(self.corpus.items[item_id], config) for item_id in item_ids]

    def commit(self, results: list[dict[str, Any]], config: RunConfig | None = None) -> Any:
        cfg = config or RunConfig()
        if cfg.dry_run:
            self.logger.event("dry_run", skipped_ledger=True)
            return None
        numerator, denominator, defects, facts = confab_totals(results)
        return self.ledger.record(
            self.generator.provider_id,
            prompt_hash(self.prompt),
            self.regression_set_id,
            numerator,
            denominator,
            defects,
            facts,
        )


def confab_totals(results: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    scored = [result for result in results if result.get("scored")]
    numerator = sum(result["confab"]["numerator"] for result in scored)
    denominator = sum(result["confab"]["denominator"] for result in scored)
    defects = sum(result["citation_defects"] for result in scored)
    facts = sum(result["fact_claims"] for result in scored)
    return numerator, denominator, defects, facts


def _mean(values: list[float | None]) -> float | None:
    kept = [value for value in values if value is not None]
    if not kept:
        return None
    return sum(kept) / len(kept)


def item_citation_recall(result: dict[str, Any]) -> float | None:
    if not result.get("scored"):
        return None
    rows = [row for row in result["rows"] if row["resolved_support"] != "abstain"]
    if not rows:
        return None
    return sum(row["citation_recall"] for row in rows) / len(rows)


def item_citation_precision(result: dict[str, Any]) -> float | None:
    if not result.get("scored"):
        return None
    rows = [row for row in result["rows"] if row["resolved_support"] != "abstain"]
    if not rows:
        return None
    return sum(row["citation_precision"] for row in rows) / len(rows)


def summarize(corpus: Corpus, results: list[dict[str, Any]], *, dry_run: bool) -> dict[str, Any]:
    by_id = {result["item_id"]: result for result in results}
    scored_items = [item for item in corpus.ordered_items() if item.batch == "scored"]
    by_label: dict[str, Any] = {}
    for label in LABELS:
        group = [by_id[item.item_id] for item in scored_items if item.construction_label == label and item.item_id in by_id]
        by_label[label] = {
            "n": len(group),
            "claim_recall": _mean([row["metrics"]["claim_recall"] if row.get("metrics") else None for row in group]),
            "faithfulness": _mean([row["metrics"]["faithfulness"] if row.get("metrics") else None for row in group]),
            "citation_recall": _mean([item_citation_recall(row) for row in group]),
            "citation_precision": _mean([item_citation_precision(row) for row in group]),
            "accuracy": _mean([1.0 if row.get("accurate") else 0.0 for row in group]) if group else None,
        }
    pooled_rows = [by_id[item.item_id] for item in scored_items if item.item_id in by_id]
    acceptance: dict[str, int] = {}
    for result in results:
        acceptance[result["acceptance"]] = acceptance.get(result["acceptance"], 0) + 1
    targeted = [item for item in corpus.ordered_items() if item.poison_target and item.item_id in by_id]
    if targeted:
        poison_rate = sum(1 for item in targeted if by_id[item.item_id].get("poison_in_context")) / len(targeted)
    else:
        poison_rate = None
    labeled = [item for item in scored_items if item.model_label]
    if labeled:
        disagree = sum(1 for item in labeled if item.model_label != item.construction_label) / len(labeled)
    else:
        disagree = None
    numerator, denominator, defects, facts = confab_totals(
        [by_id[item_id] for item_id in corpus.regression_item_ids if item_id in by_id]
    )
    report = {
        "dry_run": dry_run,
        "by_label": by_label,
        "pooled": {
            "n": len(pooled_rows),
            "claim_recall": _mean(
                [row["metrics"]["claim_recall"] if row.get("metrics") else None for row in pooled_rows]
            ),
            "faithfulness": _mean(
                [row["metrics"]["faithfulness"] if row.get("metrics") else None for row in pooled_rows]
            ),
            "accuracy": _mean([1.0 if row.get("accurate") else 0.0 for row in pooled_rows]) if pooled_rows else None,
        },
        "acceptance": acceptance,
        "poison_in_top_k": poison_rate,
        "model_label_disagreement": disagree,
        "confabulation": {"numerator": numerator, "denominator": denominator},
        "citation_defects": {"numerator": defects, "denominator": facts},
        "schema_unscored": sum(1 for result in results if not result.get("scored")),
        "items": {
            item_id: {
                "acceptance": result["acceptance"],
                "accurate": result.get("accurate"),
                "scored": result.get("scored"),
                "attempts": result.get("attempts"),
                "fail_reason": result.get("fail_reason"),
                "anti_copy": result.get("anti_copy"),
                "confab_numerator": result["confab"]["numerator"],
            }
            for item_id, result in by_id.items()
        },
    }
    require_stratified(report)
    return report
