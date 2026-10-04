"""Snapshot diffs: added, removed, changed fields, unchanged."""

from __future__ import annotations

from typing import Iterable

from .models import ChangeSet, FieldChange, Product


_COMPARE_FIELDS = (
    "title",
    "price_cents",
    "currency",
    "availability",
    "color",
    "size",
    "image_url",
    "description",
)


def diff_products(previous: Iterable[Product], current: Iterable[Product]) -> ChangeSet:
    old = {item.sku: item for item in previous}
    new = {item.sku: item for item in current}
    added = tuple(sorted(sku for sku in new if sku not in old))
    removed = tuple(sorted(sku for sku in old if sku not in new))
    changed: list[tuple[str, tuple[FieldChange, ...]]] = []
    unchanged: list[str] = []
    for sku in sorted(set(old) & set(new)):
        fields = _field_changes(old[sku], new[sku])
        if fields:
            changed.append((sku, fields))
        else:
            unchanged.append(sku)
    return ChangeSet(
        added=added,
        removed=removed,
        changed=tuple(changed),
        unchanged=tuple(unchanged),
    )


def _field_changes(before: Product, after: Product) -> tuple[FieldChange, ...]:
    changes: list[FieldChange] = []
    for name in _COMPARE_FIELDS:
        left = getattr(before, name)
        right = getattr(after, name)
        if left != right:
            changes.append(FieldChange(field=name, before=left, after=right))
    return tuple(changes)
