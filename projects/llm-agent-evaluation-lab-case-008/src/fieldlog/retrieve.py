"""Turn-level hybrid retrieval.

Session scope is applied before any ranker runs. Cosine uses a hashing
bag-of-words vector so the lab stays on the standard library. Reciprocal
rank fusion (k=60) fills the candidate list and does not pick a winner.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass

from fieldlog.memory import MemoryStore
from fieldlog.models import Edge, Episode
from fieldlog.support import Journal

RRF_K = 60
VECTOR_DIM = 64


@dataclass
class Hit:
    episode_id: str
    edge_id: str | None
    subject: str | None
    predicate: str | None
    object: str | None
    serial: int | None
    t_valid: str | None
    t_invalid: str | None
    span: str
    rank: int
    session_id: str


class BM25:
    """Okapi BM25 with k1=1.5 and b=0.75. Ties break on document id."""

    def __init__(self, docs: list[tuple[str, str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_ids = [doc_id for doc_id, _text in docs]
        self.tf: list[Counter] = []
        self.lengths: list[int] = []
        self.df: dict[str, int] = {}
        for _doc_id, text in docs:
            tokens = tokenize(text)
            counts = Counter(tokens)
            self.tf.append(counts)
            self.lengths.append(len(tokens))
            for token in counts:
                self.df[token] = self.df.get(token, 0) + 1
        self.n = len(self.doc_ids)
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0

    def score(self, query: str, index: int) -> float:
        if self.n == 0 or self.avgdl == 0:
            return 0.0
        total = 0.0
        length = self.lengths[index] or 1
        for token in set(tokenize(query)):
            freq = self.tf[index].get(token, 0)
            if freq == 0:
                continue
            df = self.df.get(token, 0)
            idf = math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))
            denom = freq + self.k1 * (1.0 - self.b + self.b * length / self.avgdl)
            total += idf * (freq * (self.k1 + 1.0)) / denom
        return total

    def rank(self, query: str) -> list[str]:
        scored = [(self.score(query, index), self.doc_ids[index]) for index in range(self.n)]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [doc_id for score, doc_id in scored if score > 0]


def tokenize(text: str) -> list[str]:
    return [part.casefold() for part in text.split() if part]


def hashing_vector(text: str) -> list[float]:
    vector = [0.0] * VECTOR_DIM
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:2], "big") % VECTOR_DIM
        sign = 1.0 if digest[2] % 2 == 0 else -1.0
        vector[bucket] += sign
    return vector


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def cosine_rank(docs: list[tuple[str, str]], query: str) -> list[str]:
    query_vector = hashing_vector(query)
    scored = []
    for doc_id, text in docs:
        scored.append((cosine(query_vector, hashing_vector(text)), doc_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [doc_id for score, doc_id in scored if score > 0]


def bfs_rank(docs: list[Episode], edges: list[Edge]) -> list[str]:
    """One-hop expansion from the most recent episodes, seeded in recency order."""

    if not docs:
        return []
    doc_ids = {episode.episode_id for episode in docs}
    seeds = list(reversed(docs[-4:]))
    ranked: list[str] = []
    seen: set[str] = set()
    subjects: set[str] = set()
    for episode in seeds:
        if episode.episode_id not in seen:
            ranked.append(episode.episode_id)
            seen.add(episode.episode_id)
        for edge in edges:
            if edge.episode_id == episode.episode_id:
                subjects.add(edge.subject)
    for edge in edges:
        if edge.subject in subjects and edge.episode_id in doc_ids and edge.episode_id not in seen:
            ranked.append(edge.episode_id)
            seen.add(edge.episode_id)
    return ranked


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for index, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + index)
    return sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))


def index_key(episode: Episode, edges: list[Edge], mode: str) -> str:
    fact_text = " ".join(
        f"{edge.subject} {edge.predicate} {edge.object}"
        for edge in edges
        if edge.episode_id == episode.episode_id
    )
    if mode == "turn":
        return episode.text
    if mode == "facts_only":
        return fact_text or episode.text
    return (episode.text + " " + fact_text).strip()


def visible_episodes(
    store: MemoryStore,
    session_id: str,
    scope: str = "session",
    user_id: str = "crew",
    window: tuple[str, str] | None = None,
) -> list[Episode]:
    """Filter by session before ranking. User scope adds explicitly shared episodes only."""

    chosen = []
    for episode in store.episodes:
        same_session = episode.session_id == session_id
        shared = scope == "user" and episode.user_scope and episode.user_id == user_id
        if not same_session and not shared:
            continue
        if window is not None:
            start, end = window
            if not (start <= episode.reference_time < end):
                continue
        chosen.append(episode)
    return chosen


def retrieve(
    store: MemoryStore,
    journal: Journal,
    session_id: str,
    query: str,
    k: int = 10,
    scope: str = "session",
    user_id: str = "crew",
    window: tuple[str, str] | None = None,
    key_mode: str = "fact_augmented",
) -> list[Hit]:
    episodes = visible_episodes(store, session_id, scope=scope, user_id=user_id, window=window)
    episode_ids = {episode.episode_id for episode in episodes}
    edges = [edge for edge in store.edges if edge.episode_id in episode_ids]
    docs = [(episode.episode_id, index_key(episode, edges, key_mode)) for episode in episodes]
    fused = reciprocal_rank_fusion(
        [
            BM25(docs).rank(query),
            cosine_rank(docs, query),
            bfs_rank(episodes, edges),
        ]
    )
    top = fused[:k]
    journal.add(event="retrieve", session_id=session_id, query=query, retrieved_ids=list(top), k=k, scope=scope)
    hits: list[Hit] = []
    for rank, episode_id in enumerate(top, start=1):
        matched = [edge for edge in edges if edge.episode_id == episode_id]
        episode = store.peek_episode(episode_id)
        if episode is None:
            continue
        if not matched:
            hits.append(
                Hit(
                    episode_id=episode_id,
                    edge_id=None,
                    subject=None,
                    predicate=None,
                    object=None,
                    serial=None,
                    t_valid=None,
                    t_invalid=None,
                    span=episode.text[:80],
                    rank=rank,
                    session_id=episode.session_id,
                )
            )
            continue
        for edge in matched:
            hits.append(
                Hit(
                    episode_id=episode_id,
                    edge_id=edge.edge_id,
                    subject=edge.subject,
                    predicate=edge.predicate,
                    object=edge.object,
                    serial=edge.serial,
                    t_valid=edge.t_valid,
                    t_invalid=edge.t_invalid,
                    span=edge.span,
                    rank=rank,
                    session_id=edge.session_id,
                )
            )
    return hits


def expand_month(phrase: str) -> tuple[str, str] | None:
    """Turn a month name in the question into a half-open UTC window."""

    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    lowered = phrase.casefold()
    year = 2026
    for name, number in months.items():
        if name in lowered:
            start = f"{year:04d}-{number:02d}-01T00:00:00Z"
            if number == 12:
                end = f"{year + 1:04d}-01-01T00:00:00Z"
            else:
                end = f"{year:04d}-{number + 1:02d}-01T00:00:00Z"
            return start, end
    return None
