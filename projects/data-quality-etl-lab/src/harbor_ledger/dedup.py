"""Sorted-neighborhood duplicate decisions with a clerical band.

Each blocking key is sorted, and a new row is compared only with the previous
``w - 1`` rows. Field agreement uses the independent Fellegi-Sunter weight:
``log2(m/u)`` on agreement and ``log2((1-m)/(1-u))`` on disagreement. A null on
either side skips that field. Two thresholds yield link, possible-link, and
non-link. Union-find receives link edges only, so a possible-link cannot bridge
two clusters.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from .models import ViewRow
from .similarity import cosine, jaro_winkler, levenshtein, monge_elkan, token_qgrams


@dataclass(frozen=True)
class PairDecision:
    left: str
    right: str
    decision: str
    weight: Decimal
    skipped: tuple[str, ...]
    left_source: int
    right_source: int


def log2_ratio(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("logarithm argument must be positive")
    return value.ln() / Decimal(2).ln()


def field_weight(agree: bool, m: Decimal, u: Decimal) -> Decimal:
    if agree:
        return log2_ratio(m / u)
    return log2_ratio((Decimal(1) - m) / (Decimal(1) - u))


def window_pairs(ordered_ids: list[str], window: int) -> list[tuple[str, str]]:
    """Pairs whose positions after the sort differ by at most ``window - 1``."""

    if window < 2:
        return []
    found = []
    for index, right in enumerate(ordered_ids):
        start = max(0, index - (window - 1))
        for cursor in range(start, index):
            found.append((ordered_ids[cursor], right))
    return found


def blocking_value(row: ViewRow, key_name: str) -> str:
    """A field's match form, or its first character for a ``<field>_prefix`` key."""

    if key_name.endswith("_prefix") and key_name not in row.match:
        text = row.match.get(key_name[: -len("_prefix")]) or ""
        return text[:1]
    return row.match.get(key_name) or ""


def candidate_pairs(rows: list[ViewRow], key_names: list[str], window: int) -> list[tuple[ViewRow, ViewRow]]:
    """Union of the window pairs from every blocking key.

    Rows are paired as objects, not looked up by key, so two physical rows that
    repeat a primary key are never collapsed into one. A pair that shares a
    primary key is a primary-key finding, not a record link, and is skipped.
    """

    seen: set[tuple[int, int]] = set()
    pairs: list[tuple[ViewRow, ViewRow]] = []
    for key_name in key_names:
        ordered = _sorted_for_key(rows, key_name)
        for left_index, right_index in window_pairs(list(range(len(ordered))), window):
            left, right = ordered[left_index], ordered[right_index]
            if left.row_id == right.row_id:
                continue
            token = tuple(sorted((left.source_row, right.source_row)))
            if token in seen:
                continue
            seen.add(token)
            pairs.append((left, right))
    return pairs


def _sorted_for_key(rows: list[ViewRow], key_name: str) -> list[ViewRow]:
    return sorted(
        (row for row in rows if row.row_id and not row.ragged),
        key=lambda row: (blocking_value(row, key_name), row.source_row, row.row_id),
    )


def decide_pair(left: ViewRow, right: ViewRow, profile: dict) -> PairDecision:
    spec = profile["dedup"]
    total = Decimal(0)
    skipped: list[str] = []
    for field in spec["fields"]:
        name = field["name"]
        left_value = left.match.get(name)
        right_value = right.match.get(name)
        if left_value is None or right_value is None or left_value == "" or right_value == "":
            skipped.append(name)
            continue
        agree = _agrees(left_value, right_value, field, spec)
        total += field_weight(agree, Decimal(field["m"]), Decimal(field["u"]))
    link_at = Decimal(spec["link_threshold"])
    possible_at = Decimal(spec["possible_threshold"])
    if total >= link_at:
        decision = "link"
    elif total >= possible_at:
        decision = "possible"
    else:
        decision = "non-link"
    first, second = (left, right) if (left.row_id, left.source_row) <= (right.row_id, right.source_row) else (right, left)
    return PairDecision(
        left=first.row_id,
        right=second.row_id,
        decision=decision,
        weight=total,
        skipped=tuple(skipped),
        left_source=first.source_row,
        right_source=second.source_row,
    )


def cluster_links(decisions: list[PairDecision], row_ids: list[str]) -> list[list[str]]:
    parents = {row_id: row_id for row_id in row_ids}

    def find(item: str) -> str:
        while parents[item] != item:
            parents[item] = parents[parents[item]]
            item = parents[item]
        return item

    def union(left: str, right: str) -> None:
        ra, rb = find(left), find(right)
        if ra == rb:
            return
        if ra < rb:
            parents[rb] = ra
        else:
            parents[ra] = rb

    for decision in decisions:
        if decision.decision != "link":
            continue
        if decision.left in parents and decision.right in parents:
            union(decision.left, decision.right)
    grouped: dict[str, list[str]] = defaultdict(list)
    for row_id in dict.fromkeys(row_ids):
        grouped[find(row_id)].append(row_id)
    clusters = [sorted(members) for members in grouped.values() if len(members) > 1]
    clusters.sort(key=lambda members: members[0])
    return clusters


def match_rows(rows: list[ViewRow], profile: dict) -> tuple[list[PairDecision], list[PairDecision], list[list[str]]]:
    spec = profile["dedup"]
    pairs = candidate_pairs(rows, list(spec["blocking_keys"]), int(spec["window"]))
    decisions = [decide_pair(left, right, profile) for left, right in pairs]
    links = [item for item in decisions if item.decision == "link"]
    review = [item for item in decisions if item.decision == "possible"]
    links.sort(key=lambda item: (item.left, item.right))
    review.sort(key=lambda item: (item.left, item.right))
    clusters = cluster_links(links, [row.row_id for row in rows if row.row_id])
    return links, review, clusters


def estimate_m(rows: list[ViewRow], match_pairs: list[tuple[str, str]], field: str) -> Decimal | None:
    by_id = {row.row_id: row for row in rows}
    agreed = 0
    compared = 0
    for left_id, right_id in match_pairs:
        left = by_id.get(left_id)
        right = by_id.get(right_id)
        if left is None or right is None:
            continue
        left_value = left.match.get(field)
        right_value = right.match.get(field)
        if left_value is None or right_value is None:
            continue
        compared += 1
        if left_value == right_value:
            agreed += 1
    if compared == 0:
        return None
    return Decimal(agreed) / Decimal(compared)


def estimate_u(
    rows: list[ViewRow],
    match_pairs: set[tuple[str, str]],
    field: str,
    seed: int,
    sample_size: int,
) -> Decimal | None:
    if len(rows) < 2 or sample_size < 1:
        return None
    rng = random.Random(seed)
    seen: set[tuple[str, str]] = set()
    agreed = 0
    compared = 0
    guard = 0
    limit = sample_size * 20
    ids = [row.row_id for row in rows if row.row_id]
    by_id = {row.row_id: row for row in rows}
    while compared < sample_size and guard < limit and len(ids) >= 2:
        guard += 1
        left_id, right_id = rng.sample(ids, 2)
        token = (left_id, right_id) if left_id < right_id else (right_id, left_id)
        if token in match_pairs or token in seen:
            continue
        seen.add(token)
        left_value = by_id[token[0]].match.get(field)
        right_value = by_id[token[1]].match.get(field)
        if left_value is None or right_value is None:
            continue
        compared += 1
        if left_value == right_value:
            agreed += 1
    if compared == 0:
        return None
    return Decimal(agreed) / Decimal(compared)


def _agrees(left: str, right: str, field: dict, spec: dict) -> bool:
    if left == right:
        return True
    kind = field["kind"]
    if kind == "short":
        if jaro_winkler(left, right) >= Decimal(spec["jaro_agree"]):
            return True
        distance = levenshtein(left, right, int(spec["edit_agree"]))
        return distance <= int(spec["edit_agree"])
    if kind == "token":
        if cosine(token_qgrams(left, int(spec["q"])), token_qgrams(right, int(spec["q"]))) >= Decimal(
            spec["cosine_agree"]
        ):
            return True
        return monge_elkan(left, right) >= Decimal(spec["monge_agree"])
    raise ValueError(f"unknown field kind {kind}")


def passes_separately(rows: list[ViewRow], key_name: str, window: int) -> set[tuple[str, str]]:
    """Pairs compared on one blocking key. Tests use this to show a miss and a later hit."""

    ids = [row.row_id for row in _sorted_for_key(rows, key_name)]
    return {tuple(sorted(pair)) for pair in window_pairs(ids, window)}
