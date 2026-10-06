"""Compare a baseline manifest with a candidate on one frozen case list."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import defaultdict
from typing import Any, Callable, Mapping, Sequence

from curbgate.desk import canonical_db
from curbgate.gate import decide, tag_score_key, validate_report
from curbgate.models import NIST_RISK_TAGS, GoldenCase, RunManifest, compare_manifests, validate_case_set
from curbgate.provider import ScriptedProvider, invoke
from curbgate.scoring import (
    SplitError,
    assert_held_out,
    bundle_metrics,
    cell_failure_rates,
    checklist_failure,
    choose_template,
    hierarchy_coverage,
    negative_template_fraction,
    paired_axis,
    reduce_trials,
    score_completion,
    score_tool_case,
    trial_rates,
)
from curbgate.spans import chat_span
from curbgate.stats import probability_summary, resolve_headline

logger = logging.getLogger("curbgate")

_TOOL_SUITES = {"agent_state", "hierarchy", "negative_control"}
_SCORE_DEFAULTS = {
    "task_correct": None,
    "schema_valid": None,
    "goal_state_match": None,
    "output_strings_ok": None,
    "policy_ok": None,
    "secret_absent": None,
    "tool_policy_ok": None,
    "goal_success": None,
    "key_order_ok": None,
    "conflict_success": None,
    "aligned_success": None,
    "checklist_fail": None,
    "expected_score": None,
    "pass_k": None,
    "pass_at": None,
}


def _uses_tools(case: GoldenCase, spec: Mapping[str, Any]) -> bool:
    return case.suite in _TOOL_SUITES or bool(spec.get("turns"))


def _outcome(scores: Mapping[str, Any]) -> str:
    for key, value in scores.items():
        if key.startswith("_"):
            continue
        if value is None:
            continue
        if isinstance(value, float) and math.isnan(value):
            continue
        return "scored"
    return "unscored"


def _trial(
    case: GoldenCase,
    epoch: int,
    outcome: str,
    scores: Mapping[str, Any],
    error_type: str | None,
    spans: list[dict[str, Any]],
    detail: Mapping[str, Any] | None,
    response_model: str | None = None,
) -> dict[str, Any]:
    stored = dict(_SCORE_DEFAULTS)
    stored.update({key: value for key, value in scores.items() if not key.startswith("_")})
    return {
        "case_id": case.id,
        "epoch": epoch,
        "outcome": outcome,
        "error_type": error_type,
        "response_model": response_model,
        "scores": stored,
        "spans": spans,
        "cluster_id": case.cluster_id,
        "template_id": case.template_id,
        "suite": case.suite,
        "suite_family": case.suite_family,
        "risk_tags": list(case.risk_tags),
        "test_type": case.test_type,
        "capability": case.capability,
        "split": case.split,
        "format_level": case.format_level,
        "detail": dict(detail or {}),
    }


class ScorerFaultGuard(Exception):
    pass


def run_side(
    cases: Sequence[GoldenCase],
    scripts: Mapping[str, Mapping[str, list[Mapping[str, Any]]]],
    manifest: RunManifest,
    ledger: Mapping[str, Any],
    side: str,
    sleeper: Callable[[float], None],
    dry_run: bool,
    include_messages: bool,
) -> list[dict[str, Any]]:
    provider = ScriptedProvider(scripts)
    trials: list[dict[str, Any]] = []
    for case in cases:
        for epoch in range(manifest.epochs):
            invoked = invoke(provider, case.id, epoch, side, manifest.max_retries, sleeper)
            user_text = str(case.expectation.get("user_text") or f"Case {case.id}")
            output_type = "text" if case.format_level == "free" else manifest.output_type
            span = chat_span(
                manifest,
                case.id,
                manifest.seed_schedule[epoch],
                invoked,
                user_text,
                output_type,
                include_messages,
            )
            if not invoked["ok"]:
                logger.info("trial case=%s epoch=%s outcome=harness_error error=%s", case.id, epoch, invoked["error_type"])
                trials.append(_trial(case, epoch, "harness_error", {}, invoked["error_type"], [span], None))
                continue
            spec = dict(invoked["spec"])
            served = str(spec.get("response_model") or manifest.response_model)
            try:
                if spec.get("scorer_fault"):
                    raise ScorerFaultGuard("scripted scorer fault")
                if _uses_tools(case, spec):
                    scores, tool_spans, detail = score_tool_case(case, spec, ledger, manifest, dry_run)
                else:
                    scores = score_completion(case, spec, manifest)
                    detail = dict(scores.pop("_detail", {}) or {})
                    tool_spans = []
                if "checklist" in spec:
                    prediction = dict(spec["checklist"])
                    if detail.get("answer") is not None:
                        # The parsed answer is the prediction on the original text.
                        prediction["label"] = detail["answer"]
                    failed = checklist_failure(case.test_type, case.expectation, prediction)
                    scores["checklist_fail"] = failed
                elif case.test_type == "mft" and scores.get("task_correct") in (0, 1):
                    scores["checklist_fail"] = 1 - int(scores["task_correct"])
            except ScorerFaultGuard as exc:
                logger.info("trial case=%s epoch=%s outcome=harness_error error=scorer_exception", case.id, epoch)
                trials.append(_trial(case, epoch, "harness_error", {}, "scorer_exception", [span], {"message": str(exc)}, served))
                continue
            except Exception as exc:  # scorer faults must not become a model miss
                logger.info("trial case=%s epoch=%s outcome=harness_error error=scorer_exception", case.id, epoch)
                trials.append(
                    _trial(case, epoch, "harness_error", {}, "scorer_exception", [span], {"message": exc.__class__.__name__}, served)
                )
                continue
            outcome = _outcome(scores)
            logger.info("trial case=%s epoch=%s outcome=%s", case.id, epoch, outcome)
            trials.append(_trial(case, epoch, outcome, scores, None, [span, *tool_spans], detail, served))
    return trials


def _mean(items: Sequence[Mapping[str, Any]], key: str, predicate: Callable[[Mapping[str, Any]], bool]) -> float | None:
    values = []
    for item in items:
        if not predicate(item):
            continue
        value = item["scores"].get(key)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        values.append(float(value))
    if not values:
        return None
    return sum(values) / len(values)


def _grouped(items: Sequence[Mapping[str, Any]], key_fn: Callable[[Mapping[str, Any]], Any], score_key: str) -> dict[str, float]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for item in items:
        raw = key_fn(item)
        keys = raw if isinstance(raw, list) else [raw]
        value = item["scores"].get(score_key)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        for key in keys:
            buckets[str(key)].append(float(value))
    return {key: sum(values) / len(values) for key, values in sorted(buckets.items())}


def _checklist_rows(trials: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for trial in trials:
        if trial["outcome"] != "scored":
            continue
        if trial["scores"].get("checklist_fail") is None:
            continue
        rows.append(
            {
                "case_id": trial["case_id"],
                "capability": trial["capability"],
                "test_type": trial["test_type"],
                "template_id": trial["template_id"],
                "failed": int(trial["scores"]["checklist_fail"]),
            }
        )
    return rows


def _bundle_block(
    baseline_items: Sequence[Mapping[str, Any]],
    candidate_items: Sequence[Mapping[str, Any]],
    manifest: RunManifest,
) -> dict[str, Any] | None:
    def take(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [item for item in items if item["suite"] == "prompt_bundle"]

    base_rows = take(baseline_items)
    cand_rows = take(candidate_items)
    if not base_rows or not cand_rows:
        return None

    def means(items: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        buckets: dict[str, list[float]] = defaultdict(list)
        for item in items:
            value = item["scores"].get("task_correct")
            if value is None:
                continue
            buckets[str(item["template_id"])].append(float(value))
        return {key: sum(values) / len(values) for key, values in buckets.items()}

    base_means = means(base_rows)
    cand_means = means(cand_rows)
    per_template = {}
    for template_id in sorted(set(base_means) & set(cand_means)):
        compared = paired_axis(
            base_rows,
            cand_rows,
            "task_correct",
            lambda item, template_id=template_id: item["template_id"] == template_id,
            manifest.alpha,
            manifest.delta,
            manifest.power,
        )
        if compared.get("available"):
            per_template[template_id] = compared
    select_ids = [item["case_id"] for item in cand_rows if item["split"] == "select"]
    confirm_ids = [item["case_id"] for item in cand_rows if item["split"] == "confirm"]
    held_out: dict[str, Any]
    try:
        assert_held_out(select_ids, confirm_ids)
        by_template: dict[str, list[float]] = defaultdict(list)
        for item in cand_rows:
            if item["split"] != "select" or item["scores"].get("task_correct") is None:
                continue
            by_template[str(item["template_id"])].append(float(item["scores"]["task_correct"]))
        chosen = choose_template(by_template) if by_template else None
        confirm_values = [
            float(item["scores"]["task_correct"])
            for item in cand_rows
            if item["split"] == "confirm"
            and item["template_id"] == chosen
            and item["scores"].get("task_correct") is not None
        ]
        held_out = {
            "ok": True,
            "chosen_template": chosen,
            "confirm_mean": (sum(confirm_values) / len(confirm_values)) if confirm_values else None,
        }
    except SplitError as exc:
        held_out = {"ok": False, "error": str(exc)}
    return {
        "baseline": bundle_metrics(base_means),
        "candidate": bundle_metrics(cand_means),
        "negative_template_fraction": negative_template_fraction(per_template),
        "per_template_side": {key: value["side"] for key, value in per_template.items()},
        "held_out": held_out,
    }


def _checklist_paired(
    baseline_items: Sequence[Mapping[str, Any]],
    candidate_items: Sequence[Mapping[str, Any]],
    manifest: RunManifest,
) -> dict[str, Any]:
    """Paired failure-rate interval per CheckList cell, clustered on template_id."""
    cells = sorted(
        {
            (str(item["capability"]), str(item["test_type"]))
            for item in baseline_items
            if item["scores"].get("checklist_fail") is not None
        }
    )
    paired = {}
    for capability, test_type in cells:
        compared = paired_axis(
            baseline_items,
            candidate_items,
            "checklist_fail",
            lambda item, capability=capability, test_type=test_type: item["capability"] == capability
            and item["test_type"] == test_type,
            manifest.alpha,
            manifest.delta,
            manifest.power,
            cluster_key="template_id",
        )
        if not compared.get("available"):
            continue
        paired[f"{capability}.{test_type}"] = {
            key: compared[key]
            for key in ("n", "baseline_mean", "candidate_mean", "mean_diff", "ci_low", "ci_high", "side", "label", "clustered", "cluster_count")
        }
    return paired


def _probability_block(items: Sequence[Mapping[str, Any]]) -> dict[str, float] | None:
    values = [float(item["scores"]["expected_score"]) for item in items if item["scores"].get("expected_score") is not None]
    if not values:
        return None
    return probability_summary(values)


def _committed(trials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Terminal scratch ledger of every candidate trial that applied a write."""
    out = {}
    for trial in trials:
        if trial["detail"].get("writes_applied"):
            out[f"{trial['case_id']}#{trial['epoch']}"] = trial["detail"]["db"]
    return out


def _key_order_clean(trials: Sequence[Mapping[str, Any]]) -> bool:
    for trial in trials:
        if trial["outcome"] == "harness_error":
            continue
        value = trial["scores"].get("key_order_ok")
        if value is None:
            continue
        if float(value) < 1.0:
            return False
    return True


def _response_drift(trials: Sequence[Mapping[str, Any]], manifest: RunManifest) -> bool:
    return any(
        trial["response_model"] is not None and trial["response_model"] != manifest.response_model
        for trial in trials
    )


def _sanitize(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


def _run_key(cases: Sequence[GoldenCase], baseline: RunManifest, candidate: RunManifest) -> str:
    payload = {
        "cases": [case.id for case in cases],
        "baseline": [baseline.manifest_id, baseline.prompt_version, baseline.prompt_sha256],
        "candidate": [candidate.manifest_id, candidate.prompt_version, candidate.prompt_sha256, candidate.declared_change],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def run_comparison(
    cases: Sequence[GoldenCase],
    scripts: Mapping[str, Mapping[str, list[Mapping[str, Any]]]],
    baseline: RunManifest,
    candidate: RunManifest,
    ledger: Mapping[str, Any],
    sleeper: Callable[[float], None] | None = None,
    dry_run: bool = False,
    include_messages: bool = True,
) -> dict[str, Any]:
    """Score both pins and decide whether the candidate may be promoted.

    `ledger` is copied before any tool call. The caller's object is left as it arrived.
    """
    if sleeper is None:
        sleeper = lambda _delay: None
    ledger_before = canonical_db(ledger)
    pin = compare_manifests(baseline, candidate)
    validate_case_set(list(cases), baseline)
    validate_case_set(list(cases), candidate)
    coverage_missing = hierarchy_coverage(cases)
    base_trials = run_side(cases, scripts, baseline, ledger, "baseline", sleeper, dry_run, include_messages)
    cand_trials = run_side(cases, scripts, candidate, ledger, "candidate", sleeper, dry_run, include_messages)
    base_items = reduce_trials(base_trials, baseline.reducer, baseline.pass_k)
    cand_items = reduce_trials(cand_trials, candidate.reducer, candidate.pass_k)
    headline_key = "pass_k" if candidate.headline_metric == "pass_k" else "task_correct"

    def headline_predicate(item: Mapping[str, Any]) -> bool:
        return item["suite"] == "core" and item["capability"] == candidate.headline_capability

    headline = paired_axis(
        base_items,
        cand_items,
        headline_key,
        headline_predicate,
        candidate.alpha,
        candidate.delta,
        candidate.power,
    )
    metrics_in_order = [
        ("stderr", float(headline["se"]) if headline.get("available") else 0.0),
        (headline_key, float(headline["candidate_mean"]) if headline.get("available") else 0.0),
    ]
    headline_name, headline_value = resolve_headline(metrics_in_order, headline_key)

    def pair_mean(key: str, predicate: Callable[[Mapping[str, Any]], bool]) -> tuple[float | None, float | None]:
        return (_mean(base_items, key, predicate), _mean(cand_items, key, predicate))

    safety = {
        "conflict_refusal": pair_mean(
            "conflict_success",
            lambda item: item["suite"] == "hierarchy" and item["scores"].get("conflict_success") is not None,
        ),
        "aligned_compliance": pair_mean(
            "aligned_success",
            lambda item: item["suite"] == "hierarchy" and item["scores"].get("aligned_success") is not None,
        ),
        "pass_k": pair_mean("pass_k", lambda item: item["suite"] == "agent_state"),
        "policy_ok": pair_mean("policy_ok", lambda item: item["scores"].get("policy_ok") is not None),
        "secret_absent": pair_mean("secret_absent", lambda item: item["scores"].get("secret_absent") is not None),
        "tool_policy_ok": pair_mean("tool_policy_ok", lambda item: item["scores"].get("tool_policy_ok") is not None),
    }
    format_names = ("reasoning", "classification")
    format_means = {}
    format_bases = {}
    for family in format_names:
        pred = lambda item, family=family: item["suite"] == "format_matrix" and item["suite_family"] == family
        format_bases[family] = _mean(base_items, "task_correct", pred)
        format_means[family] = _mean(cand_items, "task_correct", pred)
    present_tags = sorted({tag for case in cases for tag in case.risk_tags})
    tag_cells = {}
    for tag in present_tags:
        key = tag_score_key(tag)
        # policy_ok is null off the agent case; fall back to task_correct so a tagged
        # non-agent case still has a measured cell when that is the only score it has.
        cell = paired_axis(
            base_items,
            cand_items,
            key,
            lambda item, tag=tag: tag in item["risk_tags"],
            candidate.alpha,
            candidate.delta,
            candidate.power,
        )
        if not cell.get("available") and key != "task_correct":
            cell = paired_axis(
                base_items,
                cand_items,
                "task_correct",
                lambda item, tag=tag: tag in item["risk_tags"],
                candidate.alpha,
                candidate.delta,
                candidate.power,
            )
            cell["fallback_key"] = "task_correct"
        tag_cells[tag] = cell
    measured = [tag for tag, cell in tag_cells.items() if cell.get("available")]
    gaps = [tag for tag in NIST_RISK_TAGS if tag not in measured]
    rates = trial_rates(cand_trials, headline_key)
    decision = decide(
        manifest_ok=pin.ok,
        pin_reasons=pin.reasons,
        different_experiment=pin.different_experiment,
        change_kind=candidate.change_kind,
        headline=headline,
        safety=safety,
        format_means=format_means,
        format_baselines=format_bases,
        format_floors={
            "reasoning": candidate.reasoning_task_min,
            "classification": candidate.classification_task_min,
        },
        claimed_tags=candidate.claimed_risk_tags,
        measured_tags=measured,
        tag_cells=tag_cells,
        harness_error_rate=float(rates["harness_error_rate"]),
        harness_budget=candidate.harness_error_budget,
        key_order_clean=_key_order_clean(cand_trials),
        coverage_missing=coverage_missing,
    )
    if _response_drift(cand_trials, candidate) and candidate.declared_change != "model":
        decision["reasons"] = [*decision["reasons"], "response_model_drift"]
        decision["promoted"] = False
    schema_valid = {
        "reasoning": _mean(
            cand_items,
            "schema_valid",
            lambda item: item["suite"] == "format_matrix" and item["suite_family"] == "reasoning",
        ),
        "classification": _mean(
            cand_items,
            "schema_valid",
            lambda item: item["suite"] == "format_matrix" and item["suite_family"] == "classification",
        ),
    }
    report = {
        "run_key": _run_key(cases, baseline, candidate),
        "dry_run": dry_run,
        "caller_ledger_mutated": canonical_db(ledger) != ledger_before,
        "committed_ledger": None if dry_run else _committed(cand_trials),
        "manifest_ok": pin.ok,
        "pin_reasons": list(pin.reasons),
        "different_experiment": pin.different_experiment,
        "promoted": decision["promoted"],
        "reasons": decision["reasons"],
        "missing_tags": decision["missing_tags"],
        "headline": {
            "metric": headline_name,
            "value": headline_value,
            "reducer": "pass_k" if headline_key == "pass_k" else candidate.reducer,
            "label": headline.get("label"),
            "side": headline.get("side"),
            "mean_diff": headline.get("mean_diff"),
            "se": headline.get("se"),
            "naive_se": headline.get("naive_se"),
            "ci_low": headline.get("ci_low"),
            "ci_high": headline.get("ci_high"),
            "miller_low": headline.get("miller_low"),
            "miller_high": headline.get("miller_high"),
            "df": headline.get("df"),
            "correlation": headline.get("correlation"),
            "fail_count": headline.get("fail_count"),
            "n": headline.get("n"),
            "baseline_mean": headline.get("baseline_mean"),
            "candidate_mean": headline.get("candidate_mean"),
            "underpowered": headline.get("underpowered"),
        },
        "power": headline.get("power"),
        "safety": {name: {"baseline": pair[0], "candidate": pair[1]} for name, pair in safety.items()},
        "format": {
            "task_correct": {"baseline": format_bases, "candidate": format_means},
            "schema_valid_candidate": schema_valid,
            "floors": {
                "reasoning": candidate.reasoning_task_min,
                "classification": candidate.classification_task_min,
            },
        },
        "prompt_bundle": _bundle_block(base_items, cand_items, candidate),
        "checklist": {
            "baseline": cell_failure_rates(_checklist_rows(base_trials)),
            "candidate": cell_failure_rates(_checklist_rows(cand_trials)),
            "paired": _checklist_paired(base_items, cand_items, candidate),
        },
        "probability": {
            "baseline": _probability_block(base_items),
            "candidate": _probability_block(cand_items),
        },
        "grouped": {
            "risk_tag": _grouped(cand_items, lambda item: list(item["risk_tags"]), "task_correct"),
            "suite": _grouped(cand_items, lambda item: item["suite"], "task_correct"),
            "template_id": _grouped(cand_items, lambda item: item["template_id"], "task_correct"),
        },
        "risk_vector": tag_cells,
        "gaps": gaps,
        "claimed_risk_tags": list(candidate.claimed_risk_tags),
        "rates": {
            "candidate": rates,
            "baseline": trial_rates(base_trials, headline_key),
        },
        "hierarchy_coverage_missing": coverage_missing,
        "prompt": {
            "name": candidate.prompt_name,
            "baseline_version": baseline.prompt_version,
            "candidate_version": candidate.prompt_version,
            "baseline_sha256": baseline.prompt_sha256,
            "candidate_sha256": candidate.prompt_sha256,
        },
        "trials": {"baseline": base_trials, "candidate": cand_trials},
    }
    sanitized = _sanitize(report)
    validate_report(sanitized)
    logger.info(
        "compared cases=%s promoted=%s reasons=%s",
        len(cases),
        sanitized["promoted"],
        ",".join(sanitized["reasons"]),
    )
    return sanitized
