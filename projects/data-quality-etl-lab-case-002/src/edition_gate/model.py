"""Schema, dialect, and column records for the parts-catalog registry."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any, Iterable, Optional


DATATYPES = (
    "string",
    "integer",
    "long",
    "double",
    "decimal",
    "boolean",
    "date",
    "datetime",
)

PUBLICATIONS = ("absent", "delete_only", "write_only", "public")


@dataclass(frozen=True)
class Dialect:
    charset: Optional[str]
    header: str = "present"
    delimiter: str = ","
    quote: str = '"'
    escape: str = ""
    skip_rows: int = 0
    header_row_count: int = 1
    line_break: str = "lf"

    def to_plain(self) -> dict:
        return {
            "charset": self.charset,
            "header": self.header,
            "delimiter": self.delimiter,
            "quote": self.quote,
            "escape": self.escape,
            "skip_rows": self.skip_rows,
            "header_row_count": self.header_row_count,
            "line_break": self.line_break,
        }


@dataclass(frozen=True)
class Column:
    name: str
    datatype: str = "string"
    aliases: tuple = ()
    titles: tuple = ()
    required: bool = False
    has_default: bool = False
    default: Any = None
    null_tokens: tuple = ("NA",)
    decimal_char: str = "."
    group_char: str = ","
    format: Optional[str] = None
    virtual: bool = False
    publication: str = "public"
    max_length: Optional[int] = None

    def to_plain(self) -> dict:
        return {
            "name": self.name,
            "datatype": self.datatype,
            "aliases": list(self.aliases),
            "titles": [{"text": text, "language": lang} for text, lang in self.titles],
            "required": self.required,
            "has_default": self.has_default,
            "default": self.default,
            "null_tokens": list(self.null_tokens),
            "decimal_char": self.decimal_char,
            "group_char": self.group_char,
            "format": self.format,
            "virtual": self.virtual,
            "publication": self.publication,
            "max_length": self.max_length,
        }


@dataclass(frozen=True)
class Schema:
    number: int
    columns: tuple
    dialect: Dialect
    name: str = "parts_catalog"
    version_id: str = ""

    def __post_init__(self) -> None:
        if not self.version_id:
            object.__setattr__(self, "version_id", "parts-{0}".format(self.number))

    def column(self, name: str) -> Optional[Column]:
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def to_plain(self) -> dict:
        return {
            "version_id": self.version_id,
            "number": self.number,
            "name": self.name,
            "dialect": self.dialect.to_plain(),
            "columns": [col.to_plain() for col in self.columns],
        }


def _as_tuple(value: Optional[Iterable]) -> tuple:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    return tuple(value)


def column(name: str, **kwargs: Any) -> Column:
    if "aliases" in kwargs:
        kwargs["aliases"] = _as_tuple(kwargs["aliases"])
    if "null_tokens" in kwargs:
        kwargs["null_tokens"] = _as_tuple(kwargs["null_tokens"])
    if "titles" in kwargs:
        titles = []
        for item in kwargs["titles"]:
            if isinstance(item, tuple):
                titles.append(item)
            else:
                titles.append((item["text"], item.get("language") or ""))
        kwargs["titles"] = tuple(titles)
    return Column(name=name, **kwargs)


def dialect(**kwargs: Any) -> Dialect:
    base = Dialect(charset="utf-8")
    if not kwargs:
        return base
    return replace(base, **kwargs)


def schema(number: int, columns: Iterable[Column], **kwargs: Any) -> Schema:
    dialect_value = kwargs.pop("dialect", None) or dialect()
    return Schema(
        number=number,
        columns=tuple(columns),
        dialect=dialect_value,
        name=kwargs.pop("name", "parts_catalog"),
        version_id=kwargs.pop("version_id", "parts-{0}".format(number)),
    )


def replace_column(source: Schema, name: str, **changes: Any) -> Schema:
    columns = []
    found = False
    for col in source.columns:
        if col.name == name:
            columns.append(replace(col, **changes))
            found = True
        else:
            columns.append(col)
    if not found:
        raise KeyError(name)
    return replace(source, columns=tuple(columns))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
