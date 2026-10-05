"""Per-class precision, recall, and F against a cell oracle.

There is no headline F. An outlier-only injection must be allowed to score well
for the outlier detector and poorly for the constraint detector. Upper-bound
recall counts every oracle cell whose class the detector is allowed to cover,
so a perfect score on one class cannot hide a detector that cannot see another.
Training-slice and evaluation-slice figures are stored separately.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from statistics import median

COVERS = {
    "pattern_type": {"bogus", "typo"},
    "constraint": {"constraint_break", "missing"},
    "outlier": {"outlier"},
    "duplicate": {"duplicated_value"},
}

_QUANT = Decimal("0.000001")


def score_slices(
    findings: list[dict],
    oracle: list[dict],
    slices: dict[str, str],
) -> dict:
    return {
        "evaluation": score_slice(findings, oracle, slices, "eval"),
        "training": score_slice(findings, oracle, slices, "train"),
    }


def score_slice(
    findings: list[dict],
    oracle: list[dict],
    slices: dict[str, str],
    slice_name: str,
) -> dict[str, dict]:
    rows_in_slice = {row_id for row_id, name in slices.items() if name == slice_name}
    slice_oracle = [cell for cell in oracle if cell["row_id"] in rows_in_slice]
    slice_findings = [finding for finding in findings if finding["record_key"] in rows_in_slice]
    classes = set()
    for covered in COVERS.values():
        classes.update(covered)
    classes.update(cell["class"] for cell in slice_oracle)
    report: dict[str, dict] = {}
    for detector in COVERS:
        report[detector] = {
            klass: _score_detector_class(slice_findings, slice_oracle, detector, klass)
            for klass in sorted(classes)
        }
    return report


def _score_detector_class(
    findings: list[dict], oracle: list[dict], detector: str, klass: str
) -> dict:
    # Scores are per cell, as in the detection study: two findings from one
    # detector on the same cell are one prediction.
    oracle_set = {(cell["row_id"], cell["column"]) for cell in oracle if cell["class"] == klass}
    predictions = {
        (finding["record_key"], finding["column"])
        for finding in findings
        if finding["detector"] == detector and finding["class"] == klass
    }
    true_positive = len(predictions & oracle_set)
    false_positive = len(predictions - oracle_set)
    false_negative = len(oracle_set - predictions)
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    if precision + recall == 0:
        f_score = Decimal(0)
    else:
        f_score = (Decimal(2) * precision * recall) / (precision + recall)
    covers = klass in COVERS[detector]
    if covers and true_positive + false_negative:
        upper = Decimal(1)
    else:
        upper = recall
    return {
        "f": _text(f_score),
        "fn": false_negative,
        "fp": false_positive,
        "precision": _text(precision),
        "recall": _text(recall),
        "tp": true_positive,
        "upper_recall": _text(upper),
    }


def suggest_outlier_threshold(values: list[Decimal]) -> Decimal:
    """Training-slice diagnostic. The published run does not apply this number."""

    if len(values) < 2:
        return Decimal("3.5")
    center = Decimal(str(median(values)))
    deviations = [abs(value - center) for value in values]
    mad = Decimal(str(median(deviations)))
    if mad == 0:
        return Decimal("3.5")
    scores = sorted(Decimal("0.6745") * abs(value - center) / mad for value in values)
    index = min(len(scores) - 1, int(len(scores) * Decimal("0.9")))
    return scores[index]


def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal(0)
    return Decimal(numerator) / Decimal(denominator)


def _text(value: Decimal) -> str:
    return format(value.quantize(_QUANT, rounding=ROUND_HALF_EVEN), "f")
