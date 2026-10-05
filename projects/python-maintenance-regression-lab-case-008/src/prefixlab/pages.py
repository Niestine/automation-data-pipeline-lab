"""Page images. Redo applies a record only when its LSN is newer than page_lsn."""

from __future__ import annotations

from dataclasses import dataclass, field

from prefixlab.wal import LogRecord


@dataclass
class Page:
    page_id: str
    data: bytes
    page_lsn: int = 0


@dataclass
class PageStore:
    pages: dict[str, Page] = field(default_factory=dict)
    initial: dict[str, bytes] = field(default_factory=dict)

    def open_page(self, page_id: str, data: bytes) -> None:
        if not page_id:
            raise ValueError("page id is required")
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("page image must be bytes")
        blob = bytes(data)
        self.initial[page_id] = blob
        self.pages[page_id] = Page(page_id, blob, 0)

    def reset(self) -> None:
        self.pages = {
            page_id: Page(page_id, data, 0) for page_id, data in self.initial.items()
        }

    def get(self, page_id: str) -> bytes:
        try:
            return self.pages[page_id].data
        except KeyError as exc:
            raise KeyError(page_id) from exc

    def apply(self, record: LogRecord) -> bool:
        """Install ``after`` when ``page_lsn < record.lsn``. Return whether it applied."""

        if record.kind not in ("update", "clr"):
            return False
        if not record.page:
            if record.kind == "update":
                raise ValueError("update records require a page")
            return False
        page = self.pages.get(record.page)
        if page is None:
            raise KeyError(record.page)
        if page.page_lsn >= record.lsn:
            return False
        page.data = bytes(record.after)
        page.page_lsn = record.lsn
        return True
