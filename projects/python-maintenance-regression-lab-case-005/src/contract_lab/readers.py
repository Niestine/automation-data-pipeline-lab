"""Strict and tolerant object readers for composed schemas.

The strict reader sets ``unevaluatedProperties`` to false and applies it only
after annotations from ``properties``, ``patternProperties``, and
``additionalProperties`` are collected, including annotations that arrived
through ``allOf``, ``$ref``, and ``if``/``then``/``else``.

The negative control sets root ``additionalProperties`` to false. That keyword
looks only at ``properties`` and ``patternProperties`` in the same schema
object, so a property declared inside ``$ref`` is rejected. That disagreement
is the defect this reader exists to lock.

Failure records are JSON Schema detailed-output nodes. The other three output
formats are not produced. Type checks are defensive and local: a JSON boolean
is not an integer, and ``1.0`` is not accepted as an integer. This is not an
implementation of the JSON Schema validation vocabulary.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

def escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_ref(root: dict, ref: str):
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError(f"only document-local refs are supported: {ref!r}")
    node: object = root
    for raw in ref[2:].split("/"):
        token = _unescape(raw)
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit():
            node = node[int(token)]
        else:
            raise ValueError(f"unresolvable ref: {ref}")
    return node


def as_strict(schema: dict) -> dict:
    """Close the object with ``unevaluatedProperties``.

    A root ``additionalProperties: false`` is the defect the control keeps, so
    it is dropped here. A schema-valued or ``true`` ``additionalProperties`` is
    the provider's own rule and is kept.
    """

    out = copy.deepcopy(schema)
    if out.get("additionalProperties") is False:
        del out["additionalProperties"]
    out["unevaluatedProperties"] = False
    return out


def as_tolerant(schema: dict) -> dict:
    out = copy.deepcopy(schema)
    out.pop("unevaluatedProperties", None)
    return out


def additional_properties_control(schema: dict) -> dict:
    """The closed-root defect: ``additionalProperties: false`` misses ``$ref``."""

    out = copy.deepcopy(schema)
    out.pop("unevaluatedProperties", None)
    out["additionalProperties"] = False
    return out


@dataclass
class Context:
    root: dict
    resource_id: str
    keyword: str
    absolute: str
    instance: str
    stack: tuple[str, ...] = ()

    @classmethod
    def for_schema(cls, schema: dict) -> Context:
        return cls(
            root=schema,
            resource_id=str(schema.get("$id") or "") if isinstance(schema, dict) else "",
            keyword="",
            absolute="",
            instance="",
        )

    def child(
        self,
        token: str,
        *,
        instance_token: str | None = None,
        absolute_token: str | None = None,
    ) -> Context:
        instance = self.instance
        if instance_token is not None:
            instance = f"{self.instance}/{escape_pointer(instance_token)}"
        absolute_piece = token if absolute_token is None else absolute_token
        return Context(
            root=self.root,
            resource_id=self.resource_id,
            keyword=f"{self.keyword}/{token}",
            absolute=f"{self.absolute}/{absolute_piece}",
            instance=instance,
            stack=self.stack,
        )

    def for_ref(self, ref: str) -> Context:
        target = ref[1:] if ref.startswith("#") else ref
        if not target.startswith("/"):
            target = "/" + target
        return Context(
            root=self.root,
            resource_id=self.resource_id,
            keyword=f"{self.keyword}/$ref",
            absolute=target,
            instance=self.instance,
            stack=self.stack + (ref,),
        )

    def error(self, keyword_token: str, message: str, *, instance_token: str | None = None) -> dict:
        instance = self.instance
        if instance_token is not None:
            instance = f"{self.instance}/{escape_pointer(instance_token)}"
        absolute = f"{self.absolute}/{keyword_token}"
        node = {
            "valid": False,
            "keywordLocation": f"{self.keyword}/{keyword_token}",
            "instanceLocation": instance,
            "error": message,
        }
        if self.resource_id:
            node["absoluteKeywordLocation"] = f"{self.resource_id}#{absolute}"
        return node


@dataclass
class EvalResult:
    valid: bool
    annotations: set[str] = field(default_factory=set)
    errors: list[dict] = field(default_factory=list)


def _type_ok(value, expected) -> bool:
    if isinstance(expected, list):
        return any(_type_ok(value, item) for item in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _evaluate(schema, instance, ctx: Context) -> EvalResult:
    if schema is False:
        return EvalResult(False, errors=[ctx.error("schema", "Schema is boolean false.")])
    if schema is True or not isinstance(schema, dict):
        return EvalResult(True)
    annotations: set[str] = set()
    errors: list[dict] = []

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in ctx.stack:
            errors.append(ctx.error("$ref", f"Cyclic ref {ref}."))
        else:
            try:
                target = resolve_ref(ctx.root, ref)
            except ValueError as exc:
                errors.append(ctx.error("$ref", str(exc)))
            else:
                sub = _evaluate(target, instance, ctx.for_ref(ref))
                annotations |= sub.annotations
                errors.extend(sub.errors)

    for index, subschema in enumerate(schema.get("allOf") or []):
        sub = _evaluate(subschema, instance, ctx.child(f"allOf/{index}"))
        annotations |= sub.annotations
        errors.extend(sub.errors)

    if "if" in schema:
        condition = _evaluate(schema["if"], instance, ctx.child("if"))
        branch = "then" if condition.valid else "else"
        if condition.valid:
            annotations |= condition.annotations
        if branch in schema:
            sub = _evaluate(schema[branch], instance, ctx.child(branch))
            annotations |= sub.annotations
            errors.extend(sub.errors)

    if "type" in schema and not _type_ok(instance, schema["type"]):
        errors.append(ctx.error("type", f"Value does not match type {schema['type']!r}."))

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(ctx.error("enum", "Value is not in the enum."))

    if isinstance(instance, dict):
        local_named: set[str] = set()
        if "properties" in schema:
            for name, subschema in schema["properties"].items():
                if name in instance:
                    local_named.add(name)
                    sub = _evaluate(
                        subschema,
                        instance[name],
                        ctx.child("properties", absolute_token="properties").child(
                            escape_pointer(name),
                            instance_token=name,
                            absolute_token=escape_pointer(name),
                        ),
                    )
                    errors.extend(sub.errors)
        if "patternProperties" in schema:
            for pattern, subschema in schema["patternProperties"].items():
                compiled = re.compile(pattern)
                for name, value in instance.items():
                    if compiled.search(name):
                        local_named.add(name)
                        sub = _evaluate(
                            subschema,
                            value,
                            ctx.child("patternProperties").child(
                                escape_pointer(pattern),
                                instance_token=name,
                                absolute_token=escape_pointer(pattern),
                            ),
                        )
                        errors.extend(sub.errors)
        annotations |= local_named

        if "additionalProperties" in schema:
            keyword = schema["additionalProperties"]
            for name, value in instance.items():
                if name in local_named:
                    continue
                if keyword is False:
                    errors.append(
                        ctx.error(
                            "additionalProperties",
                            f"Additional property {name!r} is not allowed.",
                            instance_token=name,
                        )
                    )
                elif keyword is True:
                    annotations.add(name)
                else:
                    sub = _evaluate(
                        keyword,
                        value,
                        ctx.child("additionalProperties", instance_token=name),
                    )
                    if sub.valid:
                        annotations.add(name)
                    errors.extend(sub.errors)

        for name in schema.get("required") or []:
            if name not in instance:
                errors.append(
                    ctx.error("required", f"Required property {name!r} is missing.")
                )

        if "unevaluatedProperties" in schema:
            keyword = schema["unevaluatedProperties"]
            for name, value in instance.items():
                if name in annotations:
                    continue
                if keyword is False:
                    errors.append(
                        ctx.error(
                            "unevaluatedProperties",
                            f"Unevaluated property {name!r} is not allowed.",
                            instance_token=name,
                        )
                    )
                elif keyword is True:
                    annotations.add(name)
                else:
                    sub = _evaluate(
                        keyword,
                        value,
                        ctx.child("unevaluatedProperties", instance_token=name),
                    )
                    if sub.valid:
                        annotations.add(name)
                    errors.extend(sub.errors)

    return EvalResult(valid=not errors, annotations=annotations, errors=errors)


def validate(schema: dict, instance) -> dict:
    result = _evaluate(schema, instance, Context.for_schema(schema))
    return {
        "valid": result.valid,
        "annotations": sorted(result.annotations),
        "errors": result.errors,
    }


def project(schema: dict, instance):
    """Copy the properties the tolerant reader evaluated.

    The names come from the same annotation pass the validator uses: the
    taken ``if``/``then``/``else`` branch, ``allOf``, and referenced ``$ref``
    targets. Unknown siblings and fields from unreferenced ``$defs`` stay out
    of the business object.
    """

    if not isinstance(instance, dict):
        return instance
    tolerant = as_tolerant(schema)
    names = _evaluate(tolerant, instance, Context.for_schema(tolerant)).annotations
    return {name: value for name, value in instance.items() if name in names}
