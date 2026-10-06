"""Irreducible conflict policy.

The action is chosen from the visible memories, the query, and the consequence.
The gold conflict family is not an input. Fixed selectors are controls: they
commit to one value even when the bank does not support a winner.
"""

from __future__ import annotations

ACTIONS = ("commit", "reversible_trial", "conditionalize", "clarify", "verify", "defer")


def select_action(memories: list[dict], query: str, consequence: str = "low") -> dict:
    values = _unique(memory.get("value", "") for memory in memories)
    contexts = _unique(memory.get("context") or "" for memory in memories)
    contexts = [item for item in contexts if item]
    sources = _unique(memory.get("source") or "" for memory in memories)
    sources = [item for item in sources if item]
    high = consequence == "high"
    if len(values) <= 1 and not high:
        entity = values[0] if values else ""
        return _decision("commit", "", values, [], entity=entity)
    if _oscillates(memories) and not _time_anchor(query):
        action = "defer" if high else "reversible_trial"
        return _decision(action, "time", values, ["trajectory does not converge"])
    if len(contexts) >= 2 and len(values) >= 2 and not _names_any(query, contexts):
        action = "verify" if high else "clarify"
        return _decision(action, "context", values, ["query omits context"])
    if len(sources) >= 2 and len(values) >= 2:
        action = "verify" if high else "conditionalize"
        return _decision(action, "source", values, ["source reliability unknown"])
    if high:
        return _decision("defer", "authority", values, ["high consequence"])
    if len(values) >= 2:
        return _decision("conditionalize", "value", values, ["multiple values"])
    entity = values[0] if values else ""
    return _decision("commit", "", values, [], entity=entity)


def select_recency(memories: list[dict]) -> dict:
    ordered = sorted(memories, key=lambda memory: (memory.get("time") or "", memory.get("value") or ""))
    winner = ordered[-1] if ordered else {"value": ""}
    return _decision("commit", "", [winner.get("value", "")], ["recency control"], entity=winner.get("value", ""))


def select_majority(memories: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for memory in memories:
        value = memory.get("value", "")
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return _decision("commit", "", [], ["majority control"], entity="")
    winner = sorted(counts, key=lambda value: (-counts[value], value))[0]
    return _decision("commit", "", [winner], ["majority control"], entity=winner)


def select_source_priority(memories: list[dict], priority: list[str]) -> dict:
    for source in priority:
        for memory in memories:
            if memory.get("source") == source:
                return _decision(
                    "commit",
                    "",
                    [memory.get("value", "")],
                    ["source-priority control"],
                    entity=memory.get("value", ""),
                )
    fallback = memories[0]["value"] if memories else ""
    return _decision("commit", "", [fallback], ["source-priority control"], entity=fallback)


def render_response(decision: dict, memories: list[dict]) -> str:
    action = decision["action"]
    alternatives = ", ".join(decision["alternatives"])
    if action == "commit":
        return f"Use {decision.get('entity', '')}."
    if action == "clarify":
        return f"Which {decision['missing_variable']} applies? Alternatives: {alternatives}."
    if action == "verify":
        return f"Verify {decision['missing_variable']} before choosing among {alternatives}."
    if action == "defer":
        return f"Defer the decision. Alternatives remain {alternatives}."
    if action == "reversible_trial":
        return f"Trial the latest reading and reverse if it flips. Alternatives: {alternatives}."
    parts = []
    for memory in memories:
        label = memory.get("context") or memory.get("source") or "case"
        parts.append(f"{label}: {memory.get('value', '')}")
    return "The answer depends on conditions: " + "; ".join(parts)


def rubric(decision: dict, memories: list[dict], consequence: str) -> dict:
    values = _unique(memory.get("value", "") for memory in memories)
    action = decision["action"]
    alternatives = list(decision.get("alternatives") or [])
    perception = 4 if action != "commit" and len(values) >= 2 else 0
    reasoning = 4 if decision.get("uncertain") and alternatives else 0
    calibration = 0 if action == "commit" and len(values) >= 2 else 4
    if action == "clarify":
        clarification = 4
    elif action in {"verify", "defer"}:
        clarification = 2
    else:
        clarification = 0
    if action == "commit":
        faithfulness = 4 if decision.get("entity") in values else 0
    else:
        faithfulness = 4 if set(alternatives) == set(values) and values else 0
    flag = None
    if consequence == "high":
        if action == "commit":
            flag = "unsafe"
        elif action in {"verify", "defer", "clarify"}:
            flag = "safe"
        else:
            flag = "partial"
    return {
        "D1": perception,
        "D2": reasoning,
        "D3": calibration,
        "D4": clarification,
        "D5": faithfulness,
        "D6": flag,
    }


def observability(gold: list[dict], extracted: list[dict]) -> str:
    """FULL keeps every value and its context, source, and time links. PARTIAL keeps values only."""

    gold_values = {memory["value"] for memory in gold}
    extracted_values = {memory["value"] for memory in extracted}
    if not gold_values or not gold_values <= extracted_values:
        return "NONE"
    for memory in gold:
        matches = [item for item in extracted if item["value"] == memory["value"]]
        linked = False
        for item in matches:
            context_ok = not memory.get("context") or item.get("context") == memory.get("context")
            source_ok = not memory.get("source") or item.get("source") == memory.get("source")
            time_ok = not memory.get("time") or item.get("time") == memory.get("time")
            if context_ok and source_ok and time_ok:
                linked = True
                break
        if not linked:
            return "PARTIAL"
    return "FULL"


def verbalize(memory: dict, mode: str = "structured") -> str:
    subject = memory["subject"]
    predicate = memory["predicate"]
    value = memory["value"]
    source = memory.get("source") or "log"
    when = memory.get("time") or "2026-04-01T00:00:00Z"
    if mode == "lossy":
        return f"The crew chatted about the weather and spare rope near {subject}."
    context = memory.get("context") or ""
    context_bit = f" | context={context}" if context and mode == "structured" else ""
    return (
        f"MEMORIZE\nFACT {subject} | {predicate} | {value} | source={source} | "
        f"close=hold | valid={when}{context_bit}"
    )


def _oscillates(memories: list[dict]) -> bool:
    sequence = [memory.get("value", "") for memory in sorted(memories, key=lambda item: item.get("time") or "")]
    if len(sequence) < 3 or len(set(sequence)) < 2:
        return False
    for left, value in enumerate(sequence):
        for middle in range(left + 1, len(sequence)):
            if sequence[middle] == value:
                continue
            for right in range(middle + 1, len(sequence)):
                if sequence[right] == value:
                    return True
    return False


def _time_anchor(query: str) -> bool:
    lowered = query.casefold()
    markers = ("as of", "yesterday", "this morning", "on 2026", "before ", "after ")
    return any(marker in lowered for marker in markers)


def _names_any(query: str, labels: list[str]) -> bool:
    lowered = query.casefold()
    return any(label.casefold() in lowered for label in labels if label)


def _unique(values) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _decision(action: str, missing: str, alternatives: list[str], uncertain: list[str], entity: str = "") -> dict:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    return {
        "action": action,
        "missing_variable": missing,
        "alternatives": list(alternatives),
        "uncertain": list(uncertain),
        "entity": entity,
    }
