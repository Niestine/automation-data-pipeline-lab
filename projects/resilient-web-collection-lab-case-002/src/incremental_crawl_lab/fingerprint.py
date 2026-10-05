"""SHA-256 checksums and a 64-bit same-URL simhash.

The fingerprint follows Charikar's construction: casefold, tokenize, drop
stopwords, hash each feature, add or subtract its weight per bit, then keep
the sign. A zero coordinate becomes bit 0. Hamming distance is the popcount
of the XOR against the fingerprint stored for that same URL.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

TOKEN_RE = re.compile(r"[0-9A-Za-z]+")
_STOPWORD_PATH = Path(__file__).with_name("STOPWORDS.txt")


def load_stopwords(path: Path | None = None) -> frozenset[str]:
    text = (path or _STOPWORD_PATH).read_text(encoding="utf-8")
    return frozenset(line.strip().casefold() for line in text.splitlines() if line.strip())


def tokenize(text: str, stopwords: frozenset[str]) -> list[str]:
    return [token for token in TOKEN_RE.findall(text.casefold()) if token not in stopwords]


def simhash_from_hashes(pairs: list[tuple[int, int]]) -> int:
    """Build a fingerprint from ``(feature_hash, weight)`` pairs. Bit i is the integer's bit i."""
    vector = [0] * 64
    for feature_hash, weight in pairs:
        for bit in range(64):
            if (feature_hash >> bit) & 1:
                vector[bit] += weight
            else:
                vector[bit] -= weight
    fingerprint = 0
    for bit, coordinate in enumerate(vector):
        if coordinate > 0:
            fingerprint |= 1 << bit
    return fingerprint


def feature_hash(token: str) -> int:
    digest = hashlib.blake2s(token.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big")


def simhash_tokens(tokens: list[str]) -> int:
    if not tokens:
        return 0
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    pairs = [(feature_hash(token), weight) for token, weight in counts.items()]
    return simhash_from_hashes(pairs)


def simhash_text(text: str, stopwords: frozenset[str] | None = None) -> int:
    words = stopwords if stopwords is not None else load_stopwords()
    return simhash_tokens(tokenize(text, words))


def format_simhash(value: int) -> str:
    return f"{value:016x}"


def parse_simhash(value: str) -> int:
    return int(value, 16)


def hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def calibrate_corpus() -> dict[str, object]:
    """Distances for the checked-in catalog texts. ``simhash_k`` is not the web-scale cut of 3."""
    from incremental_crawl_lab.corpus import AD_EDIT, BASE, PARAGRAPH_EDIT, TIMESTAMP_EDIT

    words = load_stopwords()
    base = simhash_text(BASE, words)
    cosmetic = {
        "timestamp": hamming(base, simhash_text(TIMESTAMP_EDIT, words)),
        "ad": hamming(base, simhash_text(AD_EDIT, words)),
    }
    paragraph = hamming(base, simhash_text(PARAGRAPH_EDIT, words))
    k = choose_k([cosmetic["timestamp"], cosmetic["ad"]], paragraph)
    return {
        "cosmetic": cosmetic,
        "paragraph": paragraph,
        "simhash_k": k,
        "material_detection": k is not None,
    }


def choose_k(cosmetic_distances: list[int], paragraph_distance: int) -> int | None:
    """Largest k that keeps every cosmetic distance <= k and the paragraph edit above k.

    Overlapping ranges disable material detection. The web-scale cut of 3 is not
    a special case here.
    """
    if paragraph_distance < 0:
        raise ValueError("distance must be >= 0")
    worst = max(cosmetic_distances) if cosmetic_distances else 0
    if worst >= paragraph_distance:
        return None
    return paragraph_distance - 1
