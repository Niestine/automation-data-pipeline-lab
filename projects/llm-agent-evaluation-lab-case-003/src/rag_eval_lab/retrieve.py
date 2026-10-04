"""IDF-weighted lexical retriever. Deterministic tie-break on chunk id."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Chunk, Corpus
from .textutil import tokens


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    score: float
    text: str
    tags: tuple[str, ...]


class LexicalRetriever:
    """Coverage of query tokens weighted by inverse document frequency.

    The retriever id is the value swept on the retriever leaderboard.
    A round-trip filter, when used, must name a different id.
    """

    retriever_id = "lexical-v1"

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self.indexed: list[Chunk] = [chunk for chunk in corpus.chunks.values() if chunk.indexed]
        self.idf = self._idf(self.indexed)

    @staticmethod
    def _idf(chunks: list[Chunk]) -> dict[str, float]:
        docs = [set(tokens(chunk.text)) for chunk in chunks]
        total = len(docs)
        df: dict[str, int] = {}
        for doc in docs:
            for token in doc:
                df[token] = df.get(token, 0) + 1
        return {
            token: math.log((total + 1) / (count + 1)) + 1.0 for token, count in df.items()
        }

    def score(self, query: str, text: str) -> float:
        query_tokens = tokens(query)
        if not query_tokens:
            return 0.0
        present = set(tokens(text))
        denominator = 0.0
        numerator = 0.0
        for token in query_tokens:
            weight = self.idf.get(token, math.log(len(self.indexed) + 1) + 1.0)
            denominator += weight
            if token in present:
                numerator += weight
        if denominator == 0.0:
            return 0.0
        return numerator / denominator

    def retrieve(self, query: str, k: int, floor: float = 0.0) -> list[Hit]:
        scored: list[tuple[float, str, Chunk]] = []
        for chunk in self.indexed:
            value = self.score(query, chunk.text)
            if value < floor:
                continue
            scored.append((value, chunk.chunk_id, chunk))
        scored.sort(key=lambda row: (-row[0], row[1]))
        hits: list[Hit] = []
        for value, chunk_id, chunk in scored[:k]:
            hits.append(Hit(chunk_id, value, chunk.text, chunk.tags))
        return hits
