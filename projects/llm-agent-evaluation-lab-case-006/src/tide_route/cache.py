"""Exact-match success cache.

Key is (model, prompt_version, canonical query). Error statuses, including
428, 429, 431, and 511, are never stored. One model's hit is not served as
another model's answer.
"""

from __future__ import annotations

from dataclasses import dataclass

UNCACHEABLE_STATUSES = frozenset({428, 429, 431, 511})


def canonical_query(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class CacheEntry:
    text: str
    priced: float
    request_id: str


class ExactCache:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str, str], CacheEntry] = {}

    def key(self, model_id: str, prompt_version: str, query_text: str) -> tuple[str, str, str]:
        return (model_id, prompt_version, canonical_query(query_text))

    def get(self, model_id: str, prompt_version: str, query_text: str) -> CacheEntry | None:
        return self._store.get(self.key(model_id, prompt_version, query_text))

    def store(
        self,
        model_id: str,
        prompt_version: str,
        query_text: str,
        entry: CacheEntry,
    ) -> None:
        self._store[self.key(model_id, prompt_version, query_text)] = entry

    def __len__(self) -> int:
        return len(self._store)
