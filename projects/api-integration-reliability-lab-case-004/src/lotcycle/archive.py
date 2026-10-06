"""Sealed archive pages and the entry merge rule.

A page is addressed by the last entry id it contains. Membership of a
sealed page does not change. New rows land on the unsealed head.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Page:
    cursor: str | None
    entries: list[dict]
    updated: float
    prev_cursor: str | None
    next_cursor: str | None
    sealed: bool


@dataclass
class Collection:
    head: Page
    archives: dict[str, Page] = field(default_factory=dict)
    gone: set[str] = field(default_factory=set)
    forbidden: set[str] = field(default_factory=set)

    def entry_ids(self) -> set[str]:
        found = {entry["id"] for entry in self.head.entries}
        for page in self.archives.values():
            if page.cursor not in self.gone:
                found.update(entry["id"] for entry in page.entries)
        return found


def _entry_sort_key(entry: dict) -> tuple[int, str]:
    prefix, _, number = entry["id"].partition("-")
    return (int(number), prefix)


def build_collection(entries: list[dict], page_size: int) -> Collection:
    ordered = sorted(entries, key=_entry_sort_key)
    if page_size < 1:
        raise ValueError("page_size")
    if not ordered:
        head = Page(None, [], 0.0, None, None, False)
        return Collection(head=head)
    remainder = len(ordered) % page_size
    if remainder == 0:
        sealed_count = max(0, (len(ordered) // page_size) - 1)
        head_entries = ordered[sealed_count * page_size :]
    else:
        sealed_count = len(ordered) // page_size
        head_entries = ordered[sealed_count * page_size :]
    sealed_entries = ordered[: sealed_count * page_size]
    pages: list[Page] = []
    for index in range(sealed_count):
        chunk = sealed_entries[index * page_size : (index + 1) * page_size]
        cursor = chunk[-1]["id"]
        pages.append(
            Page(
                cursor=cursor,
                entries=list(chunk),
                updated=float(chunk[-1]["updated"]),
                prev_cursor=None,
                next_cursor=None,
                sealed=True,
            )
        )
    for index, page in enumerate(pages):
        if index > 0:
            page.prev_cursor = pages[index - 1].cursor
        if index + 1 < len(pages):
            page.next_cursor = pages[index + 1].cursor
    newest = pages[-1].cursor if pages else None
    head = Page(
        cursor=None,
        entries=list(head_entries),
        updated=float(head_entries[-1]["updated"]) if head_entries else 0.0,
        prev_cursor=newest,
        next_cursor=None,
        sealed=False,
    )
    return Collection(head=head, archives={page.cursor: page for page in pages if page.cursor})


def add_to_head(collection: Collection, entry: dict) -> None:
    """A concurrent insert changes the head only."""
    collection.head.entries.append(entry)
    collection.head.entries.sort(key=_entry_sort_key)
    collection.head.updated = float(entry["updated"])


def merge_entry(existing: dict | None, incoming: dict) -> dict:
    """Keep the greater entry updated. On a tie, keep the newer document."""
    if existing is None:
        return incoming
    if incoming["updated"] > existing["updated"]:
        return incoming
    if incoming["updated"] < existing["updated"]:
        return existing
    if incoming["doc_updated"] >= existing["doc_updated"]:
        return incoming
    return existing
