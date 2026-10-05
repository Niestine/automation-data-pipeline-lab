"""Symptom classes for a recovered crash image."""

from __future__ import annotations

from recovery_lab.errors import StructuralCorruption, UnavailableSchemaGap

DURABLE_MATCH = "durable_match"
DATA_LOSS = "data_loss"
MIXED_FIELDS = "mixed_fields"
STRUCTURAL_CORRUPTION = "structural_corruption"
REFUSED_BUT_RECOVERABLE = "refused_but_recoverable"
UNAVAILABLE_GAP = "unavailable_gap"

CLASSES = (
    DURABLE_MATCH,
    DATA_LOSS,
    MIXED_FIELDS,
    STRUCTURAL_CORRUPTION,
    REFUSED_BUT_RECOVERABLE,
    UNAVAILABLE_GAP,
)


def classify(
    recovered_hash: str | None,
    commit_hashes: list[str],
    correct_hash: str,
    exception: BaseException | None,
) -> str:
    """Map a reopen onto one symptom class.

    ``commit_hashes`` is the initial ledger plus each durable commit, in order.
    ``correct_hash`` is the ledger a log-only redo-then-undo would produce.
    Success is equality with that hash, not the absence of an exception.
    """
    if isinstance(exception, UnavailableSchemaGap):
        return UNAVAILABLE_GAP
    if isinstance(exception, StructuralCorruption):
        return STRUCTURAL_CORRUPTION
    if exception is not None:
        return REFUSED_BUT_RECOVERABLE
    if recovered_hash == correct_hash:
        return DURABLE_MATCH
    if (
        recovered_hash in commit_hashes
        and correct_hash in commit_hashes
        and commit_hashes.index(recovered_hash) < commit_hashes.index(correct_hash)
    ):
        return DATA_LOSS
    return MIXED_FIELDS
