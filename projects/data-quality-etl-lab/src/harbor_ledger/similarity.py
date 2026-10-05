"""Field similarity used before Fellegi-Sunter weights.

Short strings use Jaro-Winkler and a bounded Levenshtein distance. Multi-token
strings use padded character q-grams with cosine, and Monge-Elkan atomic
strings. Soundex is intentionally absent: the survey reports it is a poor
blocking key for many surnames, and it is the wrong tool for style codes.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from decimal import Decimal


def jaro(left: str, right: str) -> Decimal:
    if left == right:
        return Decimal(1)
    if not left or not right:
        return Decimal(0)
    match_distance = max(len(left), len(right)) // 2 - 1
    if match_distance < 0:
        match_distance = 0
    left_flags = [False] * len(left)
    right_flags = [False] * len(right)
    matches = 0
    for index, character in enumerate(left):
        start = max(0, index - match_distance)
        end = min(index + match_distance + 1, len(right))
        for cursor in range(start, end):
            if right_flags[cursor] or right[cursor] != character:
                continue
            left_flags[index] = True
            right_flags[cursor] = True
            matches += 1
            break
    if matches == 0:
        return Decimal(0)
    transpositions = 0
    cursor = 0
    for index, character in enumerate(left):
        if not left_flags[index]:
            continue
        while not right_flags[cursor]:
            cursor += 1
        if character != right[cursor]:
            transpositions += 1
        cursor += 1
    transpositions /= 2
    score = (
        Decimal(matches) / Decimal(len(left))
        + Decimal(matches) / Decimal(len(right))
        + (Decimal(matches) - Decimal(str(transpositions))) / Decimal(matches)
    ) / Decimal(3)
    return score


def jaro_winkler(left: str, right: str, prefix_scale: str = "0.1") -> Decimal:
    base = jaro(left, right)
    prefix = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char or prefix == 4:
            break
        prefix += 1
    scale = Decimal(prefix_scale)
    return base + Decimal(prefix) * scale * (Decimal(1) - base)


def levenshtein(left: str, right: str, limit: int) -> int:
    """Return the edit distance, or ``limit + 1`` when the distance exceeds ``limit``."""

    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        row_min = i
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            value = min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + cost)
            current.append(value)
            if value < row_min:
                row_min = value
        if row_min > limit:
            return limit + 1
        previous = current
    return previous[-1]


def token_qgrams(text: str, q: int = 3) -> Counter:
    """Character q-grams inside each atom.

    A whole-string window changes when tokens trade places. After the survey's
    tokenization step, the gram multiset of the atoms does not.
    """

    atoms = text.split()
    if not atoms:
        return qgrams(text, q)
    total: Counter = Counter()
    for atom in atoms:
        total.update(qgrams(atom, q))
    return total


def qgrams(text: str, q: int = 3) -> Counter:
    if q < 1:
        raise ValueError("q must be positive")
    pad = "#" * (q - 1)
    padded = f"{pad}{text}{pad}"
    return Counter(padded[index : index + q] for index in range(max(len(padded) - q + 1, 0)))


def cosine(left: Counter, right: Counter) -> Decimal:
    if not left or not right:
        return Decimal(0)
    dot = sum(left[gram] * right[gram] for gram in left)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0 or right_norm == 0:
        return Decimal(0)
    return Decimal(str(dot / (left_norm * right_norm)))


def atomic_strings(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text)


def monge_elkan(left: str, right: str) -> Decimal:
    """Matching atoms divided by the average atom count.

    Two atoms match when they are equal or one is a prefix of the other.
    """

    left_atoms = atomic_strings(left)
    right_atoms = atomic_strings(right)
    if not left_atoms and not right_atoms:
        return Decimal(1)
    if not left_atoms or not right_atoms:
        return Decimal(0)
    used: set[int] = set()
    matches = 0
    for atom in left_atoms:
        for index, other in enumerate(right_atoms):
            if index in used:
                continue
            if atom == other or atom.startswith(other) or other.startswith(atom):
                used.add(index)
                matches += 1
                break
    average = (Decimal(len(left_atoms)) + Decimal(len(right_atoms))) / Decimal(2)
    return Decimal(matches) / average


def edit_similarity(left: str, right: str) -> Decimal:
    if left == right:
        return Decimal(1)
    bound = max(len(left), len(right))
    if bound == 0:
        return Decimal(1)
    distance = levenshtein(left, right, bound)
    return Decimal(1) - (Decimal(distance) / Decimal(bound))
