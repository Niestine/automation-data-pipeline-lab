"""Hard cap on newly committed failing candidates."""

from __future__ import annotations

import statistics

from slip_lab.shrink import shrink


def apply_cap(blobs: list[bytes], cap: int, interesting_for) -> dict:
    """Shrink and count failures until ``cap``. Further failures overflow.

    Overflow candidates are not shrunk and are not written.
    """

    committed: list[bytes] = []
    ratios: list[float] = []
    overflow = 0
    for blob in blobs:
        interesting = interesting_for(blob)
        if not interesting(blob):
            continue
        if len(committed) >= cap:
            overflow += 1
            continue
        reduced = shrink(blob, interesting)
        if blob:
            ratios.append(len(reduced) / len(blob))
        committed.append(reduced)
    median = statistics.median(ratios) if ratios else None
    return {
        "committed": committed,
        "overflow": overflow,
        "median_shrink_ratio": median,
    }
