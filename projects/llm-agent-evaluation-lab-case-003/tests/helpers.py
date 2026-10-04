"""Path bootstrap and small payload builders for the RAG lab tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"

from rag_eval_lab.corpus import load_corpus  # noqa: E402
from rag_eval_lab.generator import FakeGenerator  # noqa: E402
from rag_eval_lab.pipeline import RagLab  # noqa: E402
from rag_eval_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper  # noqa: E402


def load_script() -> tuple[str, str, dict]:
    payload = json.loads((EXAMPLES / "generator_script.json").read_text(encoding="utf-8"))
    return payload["provider_id"], payload["prompt"], payload["items"]


def fresh_lab(script: dict | None = None, prompt: str | None = None) -> RagLab:
    corpus = load_corpus(EXAMPLES / "corpus.json")
    provider_id, default_prompt, items = load_script()
    clock = ManualClock()
    return RagLab(
        corpus,
        FakeGenerator(script if script is not None else items, provider_id),
        prompt or default_prompt,
        logger=JsonLogger(),
        clock=clock,
        sleeper=RecordingSleeper(clock),
    )


def cite(chunk_id: str, substring: str) -> dict:
    return {"chunk_id": chunk_id, "substring": substring}


def claim(
    claim_id: str | None,
    text: str,
    *,
    kind: str = "fact",
    support: str = "supported",
    citations: list | None = None,
    fallback: dict | None = None,
) -> dict:
    return {
        "claim_id": claim_id,
        "text": text,
        "kind": kind,
        "support": support,
        "citations": [] if citations is None else citations,
        "fallback": fallback,
    }


def envelope(item_id: str, claims: list, surface: str = "Synthetic surface text.") -> dict:
    return {
        "schema_version": "cited_answer_v1",
        "item_id": item_id,
        "surface_text": surface,
        "reverse_questions": ["What does the handbook record?"],
        "claims": claims,
    }
