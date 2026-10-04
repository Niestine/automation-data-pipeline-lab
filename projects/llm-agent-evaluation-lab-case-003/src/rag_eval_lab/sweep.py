"""Controlled token corpus for k, chunk size, floor, and overlap sweeps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .citations import score_citations
from .diagnostics import diagnose
from .errors import LabError, LabInputError
from .generator import TerseGenerator, TrustingGenerator
from .leaderboard import generator_board, rank_retriever, select_operating_point
from .models import LABELS, Chunk, Corpus
from .retrieve import LexicalRetriever
from .schema_gate import validate_response
from .textutil import tokens


def default_spec_path() -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "sweep_tokens.json"


def load_spec(path: Path | None = None) -> dict[str, Any]:
    spec_path = path or default_spec_path()
    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise LabInputError(f"sweep spec unreadable: {spec_path}") from exc
    return payload


def chunk_sentences(sentences: list[str], size: int, overlap: int) -> list[tuple[int, str]]:
    if size < 1:
        raise ValueError("chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be smaller than chunk size")
    step = size - overlap
    chunks: list[tuple[int, str]] = []
    index = 0
    while index < len(sentences):
        piece = sentences[index : index + size]
        if not piece:
            break
        chunks.append((index, " ".join(piece)))
        if index + size >= len(sentences):
            break
        index += step
    return chunks


def build_chunks(spec: dict[str, Any], size: int, overlap: int) -> tuple[list[Chunk], dict[str, list[tuple[str, int, int]]], set[str]]:
    sentences = spec["sentences"]
    texts = [row["text"] for row in sentences]
    pieces = chunk_sentences(texts, size, overlap)
    gold = set(spec["gold_claim_ids"])
    chunks: list[Chunk] = []
    evidences: dict[str, list[tuple[str, int, int]]] = {}
    gold_chunks: set[str] = set()
    for offset, text in pieces:
        covered = sentences[offset : offset + size]
        spans: list[tuple[str, int, int]] = []
        claim_ids: list[str] = []
        for row in covered:
            base = text.find(row["text"])
            if base < 0:
                raise LabInputError("sentence missing from chunk")
            for claim in row["claims"]:
                local = row["text"].find(claim["substring"])
                if local < 0:
                    raise LabInputError(f"marker missing for {claim['id']}")
                start = base + local
                spans.append((claim["id"], start, start + len(claim["substring"])))
                claim_ids.append(claim["id"])
        chunk_id = f"sw-{size}-{overlap}-{offset}"
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_id="sweep",
                text=text,
                tags=(),
                blocked=(),
                evidences=(),
                indexed=True,
            )
        )
        evidences[chunk_id] = spans
        if any(claim_id in gold for claim_id in claim_ids):
            gold_chunks.add(chunk_id)
    return chunks, evidences, gold_chunks


def _span_entails(
    evidences: dict[str, list[tuple[str, int, int]]],
    claim_id: str | None,
) -> Callable[[list[Any]], bool]:
    def entails(spans: list[Any]) -> bool:
        if claim_id is None:
            return False
        for span in spans:
            for planted, start, end in evidences.get(span["chunk_id"], []):
                if planted == claim_id and span["start"] <= start and span["end"] >= end:
                    return True
        return False

    return entails


def _measure(
    spec: dict[str, Any],
    chunks: list[Chunk],
    evidences: dict[str, list[tuple[str, int, int]]],
    gold_chunks: set[str],
    k: int,
    floor: float,
    generator: Any = None,
) -> dict[str, Any]:
    """Retrieve, run one frozen generator on the hits, and score its cited_answer_v1 payload."""
    corpus = Corpus(
        claims={},
        chunks={chunk.chunk_id: chunk for chunk in chunks},
        items={},
        quotas={label: 0 for label in LABELS},
        regression_item_ids=(),
        provider_memory_claim_ids=frozenset(),
    )
    hits = LexicalRetriever(corpus).retrieve(spec["question"], k, floor)
    retrieved = [hit.chunk_id for hit in hits]
    gold = set(spec["gold_claim_ids"])
    holders: dict[str, set[str]] = {}
    for chunk_id in retrieved:
        for claim_id, _start, _end in evidences.get(chunk_id, []):
            holders.setdefault(claim_id, set()).add(chunk_id)
    relevant = {chunk_id for claim_id in gold for chunk_id in holders.get(claim_id, set())}
    gold_retrieved = gold & set(holders)

    policy = generator or TrustingGenerator(evidences)
    payload = policy.complete("sweep", 0, hits, "")
    errors = validate_response(payload)
    if errors:
        raise LabError(f"{policy.provider_id} payload failed cited_answer_v1: {errors[0]}")
    units: list[str | None] = []
    entailed: list[set[str]] = []
    citation_recalls: list[int] = []
    for claim in payload["claims"]:
        if claim["kind"] == "insufficient_evidence":
            continue
        claim_id = claim["claim_id"]
        units.append(claim_id)
        entailed.append(set(holders.get(claim_id, set())) if claim_id else set())
        scored = score_citations(claim["citations"], _span_entails(evidences, claim_id))
        citation_recalls.append(scored["recall"])
    metrics = diagnose(list(spec["gold_claim_ids"]), units, entailed, retrieved, relevant, gold_retrieved)
    hit_rate = 1.0 if any(chunk_id in gold_chunks for chunk_id in retrieved) else 0.0
    citation_recall = sum(citation_recalls) / len(citation_recalls) if citation_recalls else None
    return {
        "metrics": metrics,
        "gold_chunk_hit_rate": hit_rate,
        "retrieved": retrieved,
        "citation_recall": citation_recall,
        "generator_id": policy.provider_id,
    }


def run_configuration(spec: dict[str, Any], size: int, overlap: int, k: int, floor: float) -> dict[str, Any]:
    chunks, evidences, gold_chunks = build_chunks(spec, size, overlap)
    measured = _measure(spec, chunks, evidences, gold_chunks, k, floor)
    metrics = measured["metrics"]
    return {
        "config_id": f"k{k}-s{size}-o{overlap}-f{floor}",
        "k": k,
        "chunk_size": size,
        "overlap": overlap,
        "floor": floor,
        "claim_recall": metrics["claim_recall"],
        "faithfulness": metrics["faithfulness"],
        "relevant_noise": metrics["relevant_noise"] or 0.0,
        "f1": metrics["f1"],
        "citation_recall": measured["citation_recall"],
        "context_precision": metrics["context_precision"],
        "gold_chunk_hit_rate": measured["gold_chunk_hit_rate"],
        "hallucination": metrics["hallucination"],
        "self_knowledge": metrics["self_knowledge"],
        "precision": metrics["precision"],
        "recall": metrics["recall"],
        "faith_num": metrics["faith_num"],
        "faith_den": metrics["faith_den"],
        "relevant_noise_num": metrics["relevant_noise_num"],
        "precision_num": metrics["precision_num"],
        "precision_den": metrics["precision_den"],
        "recall_num": metrics["recall_num"],
        "recall_den": metrics["recall_den"],
        "hallucination_num": metrics["hallucination_num"],
        "claim_recall_num": metrics["claim_recall_num"],
        "claim_recall_den": metrics["claim_recall_den"],
        "context_precision_num": metrics["context_precision_num"],
        "context_precision_den": metrics["context_precision_den"],
        "retrieved": measured["retrieved"],
    }


def run_generator_comparison(spec: dict[str, Any], size: int, overlap: int, k: int, floor: float) -> dict[str, Any]:
    """Swap frozen generators on one frozen retrieval. The board keeps insertion order."""
    chunks, evidences, gold_chunks = build_chunks(spec, size, overlap)
    rows = []
    for generator in (TerseGenerator(evidences), TrustingGenerator(evidences)):
        measured = _measure(spec, chunks, evidences, gold_chunks, k, floor, generator)
        metrics = measured["metrics"]
        rows.append(
            {
                "generator_id": measured["generator_id"],
                "config_id": f"k{k}-s{size}-o{overlap}-f{floor}",
                "faithfulness": metrics["faithfulness"],
                "citation_recall": measured["citation_recall"],
                "precision": metrics["precision"],
                "recall": metrics["recall"],
                "f1": metrics["f1"],
                "hallucination": metrics["hallucination"],
                "relevant_noise": metrics["relevant_noise"],
            }
        )
    return generator_board(rows)


def run_metric_sweep(spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Freeze the trusting copy policy and move only retrieval parameters."""
    loaded = spec or load_spec()
    rows = [run_configuration(loaded, 1, 0, k, 0.0) for k in (1, 2, 4, 5)]
    rows.append(run_configuration(loaded, 1, 0, 5, 0.95))
    rows.append(run_configuration(loaded, 2, 0, 1, 0.0))
    rows.append(run_configuration(loaded, 2, 1, 4, 0.0))
    rows.append(run_configuration(loaded, 2, 0, 4, 0.0))
    point = select_operating_point(rows)
    return {
        "question_tokens": tokens(loaded["question"]),
        "rows": rows,
        "by_id": {row["config_id"]: row for row in rows},
        "leaderboard": rank_retriever(rows),
        "operating_point": point,
        "generator_comparison": run_generator_comparison(
            loaded, point["chunk_size"], point["overlap"], point["k"], point["floor"]
        ),
        "overlap_tuned": False,
    }
