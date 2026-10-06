"""Parse fingerprint versus resolution fingerprint.

The parse fingerprint follows Avro Parsing Canonical Form: keep name, type,
and size; drop aliases, defaults, titles, and documentation. It identifies a
decoder cache entry. It is not a compatibility result.
"""

from __future__ import annotations

import hashlib
import json

from .model import Schema, canonical_json


def parsing_canonical(schema: Schema) -> str:
    fields = []
    for col in schema.columns:
        if col.virtual or col.publication == "absent":
            continue
        parts = [
            '"name":' + json.dumps(col.name, ensure_ascii=False),
            '"type":' + json.dumps(col.datatype, ensure_ascii=False),
        ]
        if col.max_length is not None:
            parts.append('"size":{0}'.format(int(col.max_length)))
        fields.append("{" + ",".join(parts) + "}")
    return (
        '{"name":'
        + json.dumps(schema.name, ensure_ascii=False)
        + ',"type":"record","fields":['
        + ",".join(fields)
        + "]}"
    )


def resolution_document(schema: Schema) -> dict:
    columns = []
    for col in schema.columns:
        columns.append(
            {
                "name": col.name,
                "datatype": col.datatype,
                "aliases": list(col.aliases),
                "required": col.required,
                "has_default": col.has_default,
                "default": col.default,
                "null_tokens": list(col.null_tokens),
                "format": col.format,
                "decimal_char": col.decimal_char,
                "group_char": col.group_char,
                "publication": col.publication,
                "virtual": col.virtual,
                "max_length": col.max_length,
            }
        )
    return {"name": schema.name, "columns": columns}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_fingerprint(schema: Schema) -> str:
    return _sha256(parsing_canonical(schema))


def resolution_fingerprint(schema: Schema) -> str:
    return _sha256(canonical_json(resolution_document(schema)))
