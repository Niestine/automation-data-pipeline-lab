"""History compliance, scored separately for each reader and separately from retirement.

A history is:

- ``consistent`` when every breaking release for that reader is a major upgrade
- ``leaking`` when any breaking release is a minor upgrade, a patch upgrade, a
  label change, no change, or any downgrade
- ``compatible_only`` when the history has no breaking release
- ``undecidable`` when no decided release leaks and some release carries an
  undecidable id or an unparseable version

An undecidable release is excluded from every bucket. ``leaking`` needs only
one decided leak, so a decided leak elsewhere in the history still makes it
``leaking``. ``consistent`` and ``compatible_only`` are claims about every
release, so any undecidable release turns them into ``undecidable``. A
one-release undecidable history therefore stays ``undecidable`` instead of
being rescored as ``compatible_only``.
An unnecessary major bump with only non-breaking edits is ``compatible_only``.
The study does not support a penalty for that bump.

The 517, 1,970, and 927 figures are separate best-case findings. They are not
a partition of the 3,075 studied APIs. The abstract's 16,053 new versions and
the dataset section's 15,856 versions are both stored.
"""

from __future__ import annotations

from contract_lab.changes import diff_openapi, impact_for
from contract_lab.errors import VersionParseError
from contract_lab.retirement import assess, protocol_ok
from contract_lab.versions import classify_change, parse_version

CITED = {
    "best_case_consistent_apis": 517,
    "leaking_apis": 1970,
    "never_breaking_apis": 927,
    "studied_apis": 3075,
    "abstract_new_versions": 16053,
    "dataset_section_versions": 15856,
    "change_type_total": 195,
    "breaking_change_types": 96,
    "non_breaking_change_types": 66,
    "undecidable_change_types": 33,
    "yasmin_breaking_versions": 251,
    "yasmin_deprecation_related_apis": 219,
    "yasmin_proactive_channel_apis": 3,
}


def cited_figures() -> dict[str, int]:
    return dict(CITED)


def evaluate_release(before: dict, after: dict, before_version: str, after_version: str) -> dict:
    """Diff first. A version failure does not erase the change ids."""

    change_ids = diff_openapi(before, after)
    version_kind = None
    version_error = None
    try:
        version_kind = classify_change(parse_version(before_version), parse_version(after_version))
    except VersionParseError as exc:
        version_error = str(exc)
    return {
        "change_ids": change_ids,
        "version_kind": version_kind,
        "version_error": version_error,
    }


def _normalize(release: dict) -> dict:
    if "before" in release:
        return evaluate_release(
            release["before"],
            release["after"],
            release["before_version"],
            release["after_version"],
        )
    return {
        "change_ids": list(release.get("change_ids") or []),
        "version_kind": release.get("version_kind"),
        "version_error": release.get("version_error"),
    }


def score_history(releases: list[dict], reader: str) -> str:
    saw_undecidable = False
    breaking_kinds: list[str] = []
    for release in releases:
        evaluated = _normalize(release)
        if evaluated["version_error"] or evaluated["version_kind"] is None:
            saw_undecidable = True
            continue
        impacts = [impact_for(change_id, reader) for change_id in evaluated["change_ids"]]
        if any(impact == "undecidable" for impact in impacts):
            saw_undecidable = True
            continue
        if any(impact == "breaking" for impact in impacts):
            breaking_kinds.append(evaluated["version_kind"])
    if any(kind != "major_upgrade" for kind in breaking_kinds):
        return "leaking"
    if saw_undecidable:
        return "undecidable"
    if not breaking_kinds:
        return "compatible_only"
    return "consistent"


def review_history(documents: list[dict], versions: list[str]) -> dict:
    """Compliance and retirement stay in separate keys."""

    if len(versions) != len(documents):
        raise ValueError("versions and documents must be the same length")
    releases = []
    for index in range(len(documents) - 1):
        releases.append(
            {
                "before": documents[index],
                "after": documents[index + 1],
                "before_version": versions[index],
                "after_version": versions[index + 1],
            }
        )
    evaluated = [_normalize(release) for release in releases]
    report = assess(documents)
    return {
        "compliance": {
            "strict": score_history(evaluated, "strict"),
            "tolerant": score_history(evaluated, "tolerant"),
        },
        "releases": evaluated,
        "protocol_ok": protocol_ok(report),
        "removals": [
            {
                "kind": item.kind,
                "operation": item.operation,
                "name": item.name,
                "pointer": item.pointer,
                "status": item.status,
            }
            for item in report.removals
        ],
        "impacted_by_version": report.impacted_by_version,
    }
