"""Stage 6. Exact supplier-SKU, then blocked approximate link.

Soundex is not used. Product titles are not surnames, and the survey's
Soundex figures do not transfer. Casefold is a linker step, separate from NFKC.
"""

from __future__ import annotations

from collections import Counter
import re
import unicodedata

from .normalize import nfc, nfkc


_ABBREV = {
    "millimeter": "mm",
    "millimeters": "mm",
    "stainless": "ss",
    "street": "st",
    "west": "w",
    "fourth": "4th",
}


def jaro(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    len_left = len(left)
    len_right = len(right)
    match_distance = max(len_left, len_right) // 2 - 1
    if match_distance < 0:
        match_distance = 0
    left_matches = [False] * len_left
    right_matches = [False] * len_right
    matches = 0
    for index in range(len_left):
        start = max(0, index - match_distance)
        end = min(index + match_distance + 1, len_right)
        for cursor in range(start, end):
            if right_matches[cursor] or left[index] != right[cursor]:
                continue
            left_matches[index] = True
            right_matches[cursor] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    cursor = 0
    transpositions = 0
    for index in range(len_left):
        if not left_matches[index]:
            continue
        while not right_matches[cursor]:
            cursor += 1
        if left[index] != right[cursor]:
            transpositions += 1
        cursor += 1
    transpositions /= 2
    return (
        matches / len_left
        + matches / len_right
        + (matches - transpositions) / matches
    ) / 3


def jaro_winkler(left: str, right: str, prefix_scale: float = 0.1, max_prefix: int = 4) -> float:
    base = jaro(left, right)
    prefix = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char or prefix == max_prefix:
            break
        prefix += 1
    return base + prefix * prefix_scale * (1 - base)


def token_qgrams(text: str, size: int = 2) -> list:
    tokens = text.split()
    if len(tokens) < size:
        return tokens
    return [" ".join(tokens[index : index + size]) for index in range(len(tokens) - size + 1)]


def qgram_dice(left: str, right: str, size: int = 2) -> float:
    left_counts = Counter(token_qgrams(left, size))
    right_counts = Counter(token_qgrams(right, size))
    if not left_counts and not right_counts:
        return 1.0
    overlap = sum((left_counts & right_counts).values())
    total = sum(left_counts.values()) + sum(right_counts.values())
    if total == 0:
        return 0.0
    return (2 * overlap) / total


def standardize(text: str) -> str:
    """NFKC, then casefold, then punctuation and abbreviation standardization."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    folded = re.sub(r"[^\w]+", " ", folded, flags=re.UNICODE)
    folded = re.sub(r"\s+", " ", folded).strip()
    parts = []
    for token in folded.split(" "):
        mapped = _ABBREV.get(token, token)
        if mapped:
            parts.append(mapped)
    return " ".join(parts)


def standardize_size(text: str) -> str:
    folded = nfkc(text or "").casefold().replace(" ", "")
    return folded.replace("millimeter", "mm")


def title_score(left: str, right: str) -> float:
    return (jaro(left, right) + jaro_winkler(left, right) + qgram_dice(left, right)) / 3


def _present(value) -> bool:
    return value is not None and str(value).strip() != ""


def _block_key(row: dict):
    if not (_present(row.get("supplier_id")) and _present(row.get("brand")) and _present(row.get("size"))):
        return None
    return (
        str(row["supplier_id"]),
        nfkc(str(row["brand"])).casefold(),
        standardize_size(str(row["size"])),
    )


def _compatibility_only(left: str, right: str) -> bool:
    if nfkc(left) != nfkc(right):
        return False
    return nfc(left) != nfc(right)


def _score_text(score: float) -> str:
    return "{0:.6f}".format(score)


def _choose(row: dict, candidates: list, match_at: float, non_at: float) -> dict:
    scored = []
    for candidate in candidates:
        left_title = str(row.get("title") or "")
        right_title = str(candidate.get("title") or "")
        if _compatibility_only(left_title, right_title):
            return {
                "kind": "review",
                "review_kind": "compatibility_fold",
                "score": None,
                "canonical_id": candidate["canonical_id"],
            }
        score = title_score(standardize(left_title), standardize(right_title))
        scored.append((score, candidate["canonical_id"]))
    if not scored:
        return {"kind": "new"}
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, best_id = scored[0]
    second = scored[1][0] if len(scored) > 1 else None
    if best_score >= match_at and (second is None or second < match_at):
        return {"kind": "link", "canonical_id": best_id, "score": best_score}
    if best_score >= match_at and second is not None and second >= match_at:
        return {"kind": "review", "review_kind": "review_band", "score": best_score, "canonical_id": best_id}
    if non_at < best_score < match_at:
        return {"kind": "review", "review_kind": "review_band", "score": best_score, "canonical_id": best_id}
    return {"kind": "new", "score": best_score}


def neighborhood_pairs(records: list, key_fns: list, window: int) -> list:
    """Multipass sorted neighborhood. Each record is compared with the previous w-1."""
    found = []
    seen = set()
    width = max(1, window)
    for key_fn in key_fns:
        ordered = sorted(records, key=lambda row: (key_fn(row), str(row.get("row_id"))))
        for index, row in enumerate(ordered):
            start = max(0, index - (width - 1))
            for earlier in range(start, index):
                other = ordered[earlier]
                if row.get("side") == other.get("side"):
                    continue
                incoming = row if row.get("side") == "incoming" else other
                canonical = other if incoming is row else row
                if canonical.get("side") != "canonical":
                    continue
                token = (str(incoming.get("row_id")), str(canonical.get("row_id")))
                if token in seen:
                    continue
                seen.add(token)
                found.append((incoming, canonical))
    return found


def link_rows(incoming: list, canonical: list, config: dict | None = None) -> dict:
    settings = config or {}
    match_at = float(settings.get("match_threshold", 0.92))
    non_at = float(settings.get("non_match_threshold", 0.78))
    window = int(settings.get("window", 3))
    links = []
    reviews = []
    by_exact = {}
    for row in canonical:
        if _present(row.get("supplier_id")) and _present(row.get("sku")):
            key = (str(row["supplier_id"]), str(row["sku"]))
            by_exact.setdefault(key, []).append(row)
    pending = []
    for row in incoming:
        supplier = row.get("supplier_id")
        sku = row.get("sku")
        if _present(supplier) and _present(sku):
            hits = by_exact.get((str(supplier), str(sku)), [])
            identities = sorted({hit["canonical_id"] for hit in hits})
            if len(identities) > 1:
                reviews.append(
                    {
                        "kind": "sku_collision",
                        "row_id": row["row_id"],
                        "canonical_ids": identities,
                    }
                )
                continue
            if len(identities) == 1:
                links.append(
                    {
                        "row_id": row["row_id"],
                        "canonical_id": identities[0],
                        "method": "exact",
                        "score": "1.000000",
                    }
                )
                continue
        pending.append(row)
    blocks = {}
    for row in canonical:
        key = _block_key(row)
        if key is not None:
            blocks.setdefault(key, []).append(row)
    neighborhood = []
    for row in pending:
        key = _block_key(row)
        candidates = blocks.get(key, []) if key is not None else []
        if key is None or not candidates:
            neighborhood.append(row)
            continue
        choice = _choose(row, candidates, match_at, non_at)
        if choice["kind"] == "link":
            links.append(
                {
                    "row_id": row["row_id"],
                    "canonical_id": choice["canonical_id"],
                    "method": "block",
                    "score": _score_text(choice["score"]),
                }
            )
        elif choice["kind"] == "review":
            reviews.append(
                {
                    "kind": choice["review_kind"],
                    "row_id": row["row_id"],
                    "canonical_id": choice.get("canonical_id"),
                    "score": None if choice.get("score") is None else _score_text(choice["score"]),
                }
            )
    if neighborhood:
        records = []
        for row in canonical:
            records.append(
                {
                    "row_id": row["canonical_id"],
                    "side": "canonical",
                    "supplier_id": row.get("supplier_id"),
                    "sku": row.get("sku"),
                    "brand": row.get("brand"),
                    "size": row.get("size"),
                    "title": row.get("title"),
                    "canonical_id": row["canonical_id"],
                }
            )
        for row in neighborhood:
            records.append({**row, "side": "incoming"})
        key_fns = [
            lambda item: standardize(str(item.get("title") or "")),
            lambda item: standardize_size(str(item.get("size") or "")),
            lambda item: nfkc(str(item.get("brand") or "")).casefold(),
        ]
        grouped = {}
        for incoming_row, canonical_row in neighborhood_pairs(records, key_fns, window):
            grouped.setdefault(incoming_row["row_id"], []).append(canonical_row)
        for row in neighborhood:
            candidates = grouped.get(row["row_id"], [])
            choice = _choose(row, candidates, match_at, non_at)
            if choice["kind"] == "link":
                links.append(
                    {
                        "row_id": row["row_id"],
                        "canonical_id": choice["canonical_id"],
                        "method": "neighborhood",
                        "score": _score_text(choice["score"]),
                    }
                )
            elif choice["kind"] == "review":
                reviews.append(
                    {
                        "kind": choice["review_kind"],
                        "row_id": row["row_id"],
                        "canonical_id": choice.get("canonical_id"),
                        "score": None if choice.get("score") is None else _score_text(choice["score"]),
                    }
                )
    return {"links": links, "reviews": reviews}


def exact_baseline(incoming: list, canonical: list) -> list:
    by_exact = {}
    for row in canonical:
        if _present(row.get("supplier_id")) and _present(row.get("sku")):
            key = (str(row["supplier_id"]), str(row["sku"]))
            by_exact.setdefault(key, set()).add(row["canonical_id"])
    links = []
    for row in incoming:
        if not (_present(row.get("supplier_id")) and _present(row.get("sku"))):
            continue
        hits = by_exact.get((str(row["supplier_id"]), str(row["sku"])), set())
        if len(hits) == 1:
            links.append({"row_id": row["row_id"], "canonical_id": next(iter(hits)), "method": "exact"})
    return links


def evaluate(labels: dict, links: list) -> dict:
    predicted = {item["row_id"]: item["canonical_id"] for item in links}
    true_positive = 0
    false_positive = 0
    false_negative = 0
    for row_id, truth in labels.items():
        guess = predicted.get(row_id)
        if truth is None:
            if guess is not None:
                false_positive += 1
        elif guess == truth:
            true_positive += 1
        elif guess is None:
            false_negative += 1
        else:
            false_positive += 1
            false_negative += 1
    precision = 1.0 if true_positive + false_positive == 0 else true_positive / (true_positive + false_positive)
    recall = 1.0 if true_positive + false_negative == 0 else true_positive / (true_positive + false_negative)
    return {
        "precision": precision,
        "recall": recall,
        "tp": true_positive,
        "fp": false_positive,
        "fn": false_negative,
    }
