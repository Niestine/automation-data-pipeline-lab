"""Deprecated-then-removed protocol.

An operation is the HTTP method plus the path template. It is impacted when
the operation, one of its parameters, or a response field it uses is marked
``deprecated: true``. A description that contains the marker ``Replacement:``
supplies the replacement string; the rest of the description is not mined.

Removal with no earlier mark is unmanaged, including when the version bump is
major. A sunset notice is not a deprecation mark. Child parameters and fields
that disappear because the whole operation was removed are covered by that
operation's removal record.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from contract_lab.changes import METHODS

REPLACEMENT_MARKER = "Replacement:"


def extract_replacement(description: str | None) -> str | None:
    """Return the text after the first ``Replacement:`` marker, or None."""

    if not isinstance(description, str):
        return None
    index = description.find(REPLACEMENT_MARKER)
    if index < 0:
        return None
    text = description[index + len(REPLACEMENT_MARKER) :].strip()
    return text or None


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def pointer(tokens: list[str]) -> str:
    return "#/" + "/".join(_escape(token) for token in tokens)


def _resolve(root: dict, ref: str):
    node: object = root
    for raw in ref[2:].split("/"):
        token = _unescape(raw)
        if isinstance(node, dict) and token in node:
            node = node[token]
        else:
            return None
    return node


@dataclass
class Element:
    kind: str
    operation: str
    name: str
    pointer: str
    deprecated: bool
    replacement: str | None = None


@dataclass(frozen=True)
class Removal:
    kind: str
    operation: str
    name: str
    pointer: str
    status: str


@dataclass
class RetirementReport:
    removals: list[Removal] = field(default_factory=list)
    impacted_by_version: list[list[str]] = field(default_factory=list)


def _methods(path_item: dict) -> list[tuple[str, dict]]:
    found = []
    for key, value in path_item.items():
        if key.lower() in METHODS and isinstance(value, dict):
            found.append((key.lower(), value))
    return found


def _response_schema(operation: dict):
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    ok = responses.get("200")
    if not isinstance(ok, dict):
        return None
    content = ok.get("content")
    if not isinstance(content, dict):
        return None
    media = content.get("application/json")
    if not isinstance(media, dict):
        return None
    schema = media.get("schema")
    return schema if isinstance(schema, dict) else None


def _walk_fields(schema, tokens: list[str], schema_root: dict, schema_tokens: list[str], refs: frozenset[str], found: list[Element], operation: str, prefix: str = "") -> None:
    """Index response fields. Nested names are dotted, so ``a.name`` and ``name`` stay apart.

    ``refs`` is the chain of ``$ref`` values on the current path. It stops a
    cycle without skipping a definition that two sibling properties share.
    """

    if not isinstance(schema, dict):
        return
    for name, sub in (schema.get("properties") or {}).items():
        if not isinstance(sub, dict):
            continue
        deprecated = sub.get("deprecated") is True
        found.append(
            Element(
                kind="response_field",
                operation=operation,
                name=prefix + name,
                pointer=pointer(tokens + ["properties", name]),
                deprecated=deprecated,
                replacement=extract_replacement(sub.get("description")) if deprecated else None,
            )
        )
        _walk_fields(sub, tokens + ["properties", name], schema_root, schema_tokens, refs, found, operation, f"{prefix}{name}.")
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/") and ref not in refs:
        target = _resolve(schema_root, ref)
        target_tokens = schema_tokens + [_unescape(part) for part in ref[2:].split("/")]
        if isinstance(target, dict):
            _walk_fields(target, target_tokens, schema_root, schema_tokens, refs | {ref}, found, operation, prefix)
    for index, sub in enumerate(schema.get("allOf") or []):
        _walk_fields(sub, tokens + ["allOf", str(index)], schema_root, schema_tokens, refs, found, operation, prefix)


def index_document(document: dict) -> dict[tuple[str, str, str], Element]:
    found: dict[tuple[str, str, str], Element] = {}
    for path, item in (document.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in _methods(item):
            label = f"{method.upper()} {path}"
            deprecated = operation.get("deprecated") is True
            op_element = Element(
                kind="operation",
                operation=label,
                name=label,
                pointer=pointer(["paths", path, method]),
                deprecated=deprecated,
                replacement=extract_replacement(operation.get("description")) if deprecated else None,
            )
            found[(op_element.kind, op_element.operation, op_element.name)] = op_element
            for index, param in enumerate(operation.get("parameters") or []):
                if not isinstance(param, dict):
                    continue
                param_deprecated = param.get("deprecated") is True
                name = f"{param.get('in')}:{param.get('name')}"
                element = Element(
                    kind="parameter",
                    operation=label,
                    name=name,
                    pointer=pointer(["paths", path, method, "parameters", str(index)]),
                    deprecated=param_deprecated,
                    replacement=extract_replacement(param.get("description")) if param_deprecated else None,
                )
                found[(element.kind, element.operation, element.name)] = element
            schema = _response_schema(operation)
            if schema is None:
                continue
            schema_tokens = ["paths", path, method, "responses", "200", "content", "application/json", "schema"]
            fields: list[Element] = []
            _walk_fields(schema, schema_tokens, schema, schema_tokens, frozenset(), fields, label)
            for element in fields:
                found[(element.kind, element.operation, element.name)] = element
    return found


def deprecated_marks(document: dict, method: str, path: str) -> list[Element]:
    label = f"{method.upper()} {path}"
    marks = [
        element
        for element in index_document(document).values()
        if element.operation == label and element.deprecated
    ]
    order = {"operation": 0, "parameter": 1, "response_field": 2}
    marks.sort(key=lambda item: (order.get(item.kind, 9), item.pointer))
    return marks


def assess(documents: list[dict]) -> RetirementReport:
    seen: dict[tuple[str, str, str], Element] = {}
    removals: list[Removal] = []
    impacted_by_version: list[list[str]] = []
    for document in documents:
        current = index_document(document)
        impacted = sorted({element.operation for element in current.values() if element.deprecated})
        impacted_by_version.append(impacted)
        pending = [key for key in seen if key not in current]
        removed_operations = {seen[key].operation for key in pending if seen[key].kind == "operation"}
        for key in pending:
            element = seen.pop(key)
            if element.kind != "operation" and element.operation in removed_operations:
                continue
            removals.append(
                Removal(
                    kind=element.kind,
                    operation=element.operation,
                    name=element.name,
                    pointer=element.pointer,
                    status="managed" if element.deprecated else "unmanaged",
                )
            )
        for key, element in current.items():
            previous = seen.get(key)
            if previous is None:
                seen[key] = element
                continue
            if element.deprecated:
                previous.deprecated = True
                if element.replacement:
                    previous.replacement = element.replacement
            previous.pointer = element.pointer
    return RetirementReport(removals=removals, impacted_by_version=impacted_by_version)


def protocol_ok(report: RetirementReport) -> bool:
    return not any(item.status == "unmanaged" for item in report.removals)
