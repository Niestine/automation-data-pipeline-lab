"""Token overlap, bag-of-words cosine, and substring offsets."""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def bow_cosine(left: str, right: str) -> float:
    a = Counter(tokens(left))
    b = Counter(tokens(right))
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    dot = sum(a[k] * b[k] for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def mean_cosine(question: str, reverses: list[str]) -> float:
    if not reverses:
        return 0.0
    return sum(bow_cosine(question, item) for item in reverses) / len(reverses)


def find_span(text: str, substring: str) -> tuple[int, int]:
    start = text.find(substring)
    if start < 0:
        raise ValueError(f"substring not found: {substring!r}")
    return start, start + len(substring)


def spans_overlap(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


def near_copy(surface: str, chunk_text: str) -> bool:
    """True when the surface reproduces most of one chunk, not a short extract."""
    surface_tokens = tokens(surface)
    chunk_tokens = tokens(chunk_text)
    if len(surface_tokens) < 6 or len(chunk_tokens) < 6:
        return False
    if len(surface_tokens) < 0.75 * len(chunk_tokens):
        return False
    covered = len(set(surface_tokens) & set(chunk_tokens)) / len(set(chunk_tokens))
    return covered >= 0.75


def harmonic(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)
