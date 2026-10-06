"""Golden cases, run manifests, and the pin check that makes a version comparable."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

NIST_RISK_TAGS = (
    "CBRN information or capabilities",
    "confabulation",
    "dangerous, violent, or hateful content",
    "data privacy",
    "environmental impacts",
    "harmful bias and homogenization",
    "human-AI configuration",
    "information integrity",
    "information security",
    "intellectual property",
    "obscene, degrading, and/or abusive content",
    "value chain and component integration",
)

SUITES = {
    "core",
    "invariance",
    "directional",
    "prompt_bundle",
    "agent_state",
    "format_matrix",
    "hierarchy",
    "negative_control",
}
TEST_TYPES = {"mft", "inv", "dir"}
DECLARED_CHANGES = {"none", "prompt", "schema", "model", "tool_policy", "temperature"}
CHANGE_KINDS = {"improvement", "no_regression"}
SCORE_AXES = {"sampled", "probability"}
FORMAT_LEVELS = {"free", "loose", "constrained"}

_CHANGE_OPENS = {
    "none": set(),
    "prompt": {"prompt_version"},
    "schema": {"schema_id", "schema_key_order"},
    "model": {"request_model", "response_model"},
    "tool_policy": {"tool_policy_version"},
    "temperature": {"temperature"},
}


def _require(mapping: Mapping[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in mapping:
        raise ValueError(f"curbgate: missing {key}")
    value = mapping[key]
    if not isinstance(value, kind):
        raise ValueError(f"curbgate: {key} has the wrong type")
    return value


def prompt_fingerprint(templates: Mapping[str, str]) -> str:
    payload = json.dumps(dict(templates), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GoldenCase:
    id: str
    cluster_id: str
    template_id: str
    suite: str
    prompt_name: str
    schema_id: str
    schema_key_order: tuple[str, ...]
    risk_tags: tuple[str, ...]
    test_type: str
    expectation: dict[str, Any]
    suite_family: str
    privilege_case: dict[str, str] | None
    format_level: str
    split: str | None
    score_axis: str
    capability: str
    plant_canary: bool

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "GoldenCase":
        case_id = str(_require(raw, "id", str))
        suite = str(_require(raw, "suite", str))
        test_type = str(_require(raw, "test_type", str))
        if suite not in SUITES:
            raise ValueError(f"curbgate: unknown suite {suite}")
        if test_type not in TEST_TYPES:
            raise ValueError(f"curbgate: unknown test_type {test_type}")
        tags = tuple(_require(raw, "risk_tags", list))
        for tag in tags:
            if tag not in NIST_RISK_TAGS:
                raise ValueError(f"curbgate: unknown risk tag {tag}")
        order = tuple(_require(raw, "schema_key_order", list))
        if not order or any(not isinstance(item, str) or not item for item in order):
            raise ValueError("curbgate: schema_key_order must be non-empty strings")
        axis = str(raw.get("score_axis") or "sampled")
        if axis not in SCORE_AXES:
            raise ValueError(f"curbgate: unknown score_axis {axis}")
        level = str(raw.get("format_level") or "constrained")
        if level not in FORMAT_LEVELS:
            raise ValueError(f"curbgate: unknown format_level {level}")
        family = str(raw.get("suite_family") or "classification")
        privilege = raw.get("privilege_case")
        if privilege is not None:
            if not isinstance(privilege, dict):
                raise ValueError("curbgate: privilege_case must be an object")
            for field in ("boundary", "tier", "alignment"):
                if field not in privilege:
                    raise ValueError(f"curbgate: privilege_case missing {field}")
            if privilege["alignment"] not in {"aligned", "conflict"}:
                raise ValueError("curbgate: privilege alignment must be aligned or conflict")
            if privilege["tier"] not in {"user", "tool"}:
                raise ValueError("curbgate: privilege tier must be user or tool")
        expectation = _require(raw, "expectation", dict)
        split = raw.get("split")
        if split is not None and split not in {"select", "confirm"}:
            raise ValueError("curbgate: split must be select or confirm")
        if axis == "probability" and (
            family == "reasoning"
            or suite in {"agent_state", "format_matrix", "hierarchy", "negative_control"}
        ):
            raise ValueError("curbgate: probability axis is only for closed single-token items")
        return GoldenCase(
            id=case_id,
            cluster_id=str(_require(raw, "cluster_id", str)),
            template_id=str(_require(raw, "template_id", str)),
            suite=suite,
            prompt_name=str(_require(raw, "prompt_name", str)),
            schema_id=str(_require(raw, "schema_id", str)),
            schema_key_order=order,
            risk_tags=tags,
            test_type=test_type,
            expectation=dict(expectation),
            suite_family=family,
            privilege_case=dict(privilege) if privilege else None,
            format_level=level,
            split=split,
            score_axis=axis,
            capability=str(raw.get("capability") or "permit_decision"),
            plant_canary=bool(raw.get("plant_canary") or False),
        )


@dataclass(frozen=True)
class RunManifest:
    manifest_id: str
    provider_name: str
    request_model: str
    response_model: str
    temperature: float
    top_p: float
    seed_schedule: tuple[int, ...]
    epochs: int
    reducer: str
    pass_k: int
    headline_metric: str
    headline_capability: str
    max_tokens: int
    prompt_name: str
    prompt_version: str
    prompt_templates: dict[str, str]
    schema_id: str
    schema_key_order: tuple[str, ...]
    declared_change: str
    alpha: float
    power: float
    delta: float
    harness_error_budget: float
    claimed_risk_tags: tuple[str, ...]
    change_kind: str
    output_type: str
    tool_policy_version: str
    max_actions: int
    max_retries: int
    reasoning_task_min: float
    classification_task_min: float

    @property
    def prompt_sha256(self) -> str:
        return prompt_fingerprint(self.prompt_templates)

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "RunManifest":
        epochs = int(_require(raw, "epochs", int))
        seeds = tuple(int(item) for item in _require(raw, "seed_schedule", list))
        if epochs < 1:
            raise ValueError("curbgate: epochs must be >= 1")
        if len(seeds) != epochs:
            raise ValueError("curbgate: seed_schedule length must equal epochs")
        temperature = float(_require(raw, "temperature", (int, float)))
        top_p = float(_require(raw, "top_p", (int, float)))
        alpha = float(_require(raw, "alpha", (int, float)))
        power = float(_require(raw, "power", (int, float)))
        delta = float(_require(raw, "delta", (int, float)))
        budget = float(_require(raw, "harness_error_budget", (int, float)))
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("curbgate: temperature out of range")
        if not 0.0 < top_p <= 1.0:
            raise ValueError("curbgate: top_p out of range")
        if not 0.0 < alpha < 1.0 or not 0.0 < power < 1.0:
            raise ValueError("curbgate: alpha and power must lie in (0, 1)")
        if delta <= 0.0:
            raise ValueError("curbgate: delta must be positive")
        if not 0.0 <= budget <= 1.0:
            raise ValueError("curbgate: harness_error_budget must lie in [0, 1]")
        declared = str(_require(raw, "declared_change", str))
        kind = str(_require(raw, "change_kind", str))
        if declared not in DECLARED_CHANGES:
            raise ValueError(f"curbgate: unknown declared_change {declared}")
        if kind not in CHANGE_KINDS:
            raise ValueError(f"curbgate: unknown change_kind {kind}")
        reducer = str(_require(raw, "reducer", str))
        if reducer not in {"mean", "majority", "pass_k"}:
            raise ValueError("curbgate: reducer must be mean, majority, or pass_k")
        headline = str(_require(raw, "headline_metric", str))
        if headline not in {"task_correct", "pass_k"}:
            raise ValueError("curbgate: headline_metric must be task_correct or pass_k")
        templates = dict(_require(raw, "prompt_templates", dict))
        if not templates or any(not isinstance(k, str) or not isinstance(v, str) for k, v in templates.items()):
            raise ValueError("curbgate: prompt_templates must map names to strings")
        tags = tuple(_require(raw, "claimed_risk_tags", list))
        for tag in tags:
            if tag not in NIST_RISK_TAGS:
                raise ValueError(f"curbgate: unknown claimed risk tag {tag}")
        order = tuple(_require(raw, "schema_key_order", list))
        pass_k = int(_require(raw, "pass_k", int))
        if pass_k < 1:
            raise ValueError("curbgate: pass_k must be >= 1")
        if pass_k > epochs:
            raise ValueError("curbgate: pass_k cannot exceed epochs")
        return RunManifest(
            manifest_id=str(_require(raw, "manifest_id", str)),
            provider_name=str(_require(raw, "provider_name", str)),
            request_model=str(_require(raw, "request_model", str)),
            response_model=str(_require(raw, "response_model", str)),
            temperature=temperature,
            top_p=top_p,
            seed_schedule=seeds,
            epochs=epochs,
            reducer=reducer,
            pass_k=pass_k,
            headline_metric=headline,
            headline_capability=str(raw.get("headline_capability") or "permit_decision"),
            max_tokens=int(_require(raw, "max_tokens", int)),
            prompt_name=str(_require(raw, "prompt_name", str)),
            prompt_version=str(_require(raw, "prompt_version", str)),
            prompt_templates=templates,
            schema_id=str(_require(raw, "schema_id", str)),
            schema_key_order=order,
            declared_change=declared,
            alpha=alpha,
            power=power,
            delta=delta,
            harness_error_budget=budget,
            claimed_risk_tags=tags,
            change_kind=kind,
            output_type=str(_require(raw, "output_type", str)),
            tool_policy_version=str(_require(raw, "tool_policy_version", str)),
            max_actions=int(raw.get("max_actions") or 8),
            max_retries=int(raw.get("max_retries") if raw.get("max_retries") is not None else 2),
            reasoning_task_min=float(raw.get("reasoning_task_min") if raw.get("reasoning_task_min") is not None else 0.5),
            classification_task_min=float(
                raw.get("classification_task_min") if raw.get("classification_task_min") is not None else 0.5
            ),
        )


@dataclass(frozen=True)
class PinResult:
    ok: bool
    reasons: tuple[str, ...]
    different_experiment: bool
    opened: tuple[str, ...]


def _pin_map(manifest: RunManifest) -> dict[str, object]:
    return {
        "provider_name": manifest.provider_name,
        "request_model": manifest.request_model,
        "response_model": manifest.response_model,
        "temperature": manifest.temperature,
        "top_p": manifest.top_p,
        "seed_schedule": manifest.seed_schedule,
        "epochs": manifest.epochs,
        "reducer": manifest.reducer,
        "pass_k": manifest.pass_k,
        "headline_metric": manifest.headline_metric,
        "headline_capability": manifest.headline_capability,
        "max_tokens": manifest.max_tokens,
        "prompt_name": manifest.prompt_name,
        "prompt_version": manifest.prompt_version,
        "schema_id": manifest.schema_id,
        "schema_key_order": manifest.schema_key_order,
        "alpha": manifest.alpha,
        "power": manifest.power,
        "delta": manifest.delta,
        "harness_error_budget": manifest.harness_error_budget,
        "claimed_risk_tags": manifest.claimed_risk_tags,
        "output_type": manifest.output_type,
        "tool_policy_version": manifest.tool_policy_version,
        "max_actions": manifest.max_actions,
        "max_retries": manifest.max_retries,
        "reasoning_task_min": manifest.reasoning_task_min,
        "classification_task_min": manifest.classification_task_min,
    }


def compare_manifests(baseline: RunManifest, candidate: RunManifest) -> PinResult:
    """Two runs are comparable only when every pin matches except the declared change.

    A prompt-body edit with the same prompt version is rejected even if the
    declared change is prompt. A temperature difference is a different
    experiment unless temperature itself is the declared change; the release
    gate still refuses to treat that experiment as a prompt promotion.
    """
    reasons: list[str] = []
    if baseline.declared_change != "none":
        reasons.append("baseline_must_declare_none")
    opened = set(_CHANGE_OPENS[candidate.declared_change])
    if baseline.prompt_sha256 != candidate.prompt_sha256 and baseline.prompt_version == candidate.prompt_version:
        reasons.append("prompt_text_without_version_bump")
    if baseline.prompt_sha256 != candidate.prompt_sha256 and candidate.declared_change != "prompt":
        reasons.append("undeclared_prompt_text")
    if baseline.prompt_version != candidate.prompt_version and candidate.declared_change != "prompt":
        reasons.append("undeclared_prompt_version")
    pins = _pin_map(baseline)
    other = _pin_map(candidate)
    drifted = sorted(key for key, value in pins.items() if other[key] != value)
    for key in drifted:
        if key not in opened:
            reasons.append(f"undeclared_{key}")
    different = baseline.temperature != candidate.temperature
    if different and candidate.declared_change != "temperature":
        reasons.append("temperature_differs")
    prompt_moved = baseline.prompt_sha256 != candidate.prompt_sha256
    if opened and not any(key in drifted for key in opened) and not (candidate.declared_change == "prompt" and prompt_moved):
        reasons.append("declared_change_not_observed")
    # temperature_differs duplicates undeclared_temperature. Keep the explicit code.
    dedup: list[str] = []
    for reason in reasons:
        if reason not in dedup:
            dedup.append(reason)
    return PinResult(
        ok=not dedup,
        reasons=tuple(dedup),
        different_experiment=different and candidate.declared_change == "temperature",
        opened=tuple(sorted(opened)),
    )


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_case_set(cases: list[GoldenCase], manifest: RunManifest) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"curbgate: duplicate case id {case.id}")
        seen.add(case.id)
        if case.prompt_name != manifest.prompt_name:
            raise ValueError(f"curbgate: case {case.id} prompt_name does not match the manifest")
        if case.template_id not in manifest.prompt_templates:
            raise ValueError(f"curbgate: case {case.id} template is not in the manifest")
        if case.score_axis == "probability" and case.suite_family == "reasoning":
            raise ValueError("curbgate: probability axis on a reasoning case")
    missing = [tag for tag in manifest.claimed_risk_tags if not any(tag in case.risk_tags for case in cases)]
    if missing:
        raise ValueError("curbgate: claimed risk tag has no case: " + ", ".join(missing))
