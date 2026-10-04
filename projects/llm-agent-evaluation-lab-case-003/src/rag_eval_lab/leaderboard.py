"""Two boards. Only the retriever sweep is allowed to rank systems."""

from __future__ import annotations

from typing import Any

from .errors import LabError

RETRIEVER_SORT_KEYS = ("claim_recall", "gold_chunk_hit_rate")


def rank_retriever(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(
        rows,
        key=lambda row: (
            -float(row["claim_recall"]),
            -float(row["gold_chunk_hit_rate"]),
            str(row.get("config_id", "")),
        ),
    )
    return {
        "decisive": True,
        "sort_keys": list(RETRIEVER_SORT_KEYS),
        "diagnostic_columns": ["context_precision"],
        "rows": ranked,
    }


def generator_board(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep insertion order. Synthetic generator order is not a winner list."""
    return {
        "decisive": False,
        "winner": None,
        "sort_keys": [],
        "rows": [dict(row) for row in rows],
    }


def rank_generators(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    del rows
    raise LabError("generator order on synthetic answers is non-decisive")


def assert_round_trip(swept_retriever_id: str, round_trip_retriever_id: str | None) -> None:
    if round_trip_retriever_id is not None and round_trip_retriever_id == swept_retriever_id:
        raise LabError("round-trip retriever must differ from the retriever under test")


def select_operating_point(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose k, chunk size, and floor on the overlap-zero slice. Overlap is not tuned."""
    pool = [row for row in rows if int(row.get("overlap", 0)) == 0]
    if not pool:
        raise LabError("overlap-zero slice is empty")
    pool.sort(
        key=lambda row: (
            -float(row["claim_recall"]),
            -float(row["gold_chunk_hit_rate"]),
            str(row.get("config_id", "")),
        )
    )
    chosen = dict(pool[0])
    chosen["overlap_tuned"] = False
    return chosen
