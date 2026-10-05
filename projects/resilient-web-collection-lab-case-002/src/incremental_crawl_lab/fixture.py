"""Local origin. Responses never leave the process.

Conditional GET uses weak ETag comparison. If-None-Match is decided before
If-Modified-Since. Content-Encoding is applied on the way out so the crawler
has to remove it before hashing.
"""

from __future__ import annotations

import gzip
import json
import zlib
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from incremental_crawl_lab.client import Response
from incremental_crawl_lab.codec import compress_lzw
from incremental_crawl_lab.corpus import ROLES
from incremental_crawl_lab.errors import OriginTimeout
from incremental_crawl_lab.observe import etags_match, parse_etag_list, split_content_type, split_etag


@dataclass
class Page:
    path: str
    body: bytes
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    encoding: str = "identity"
    etag: str | None = None
    weak: bool = False
    last_modified: str | None = None


@dataclass
class Fault:
    timeout: bool = False
    status: int = 503
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


def encode_wire(body: bytes, encoding: str) -> tuple[bytes, str | None]:
    if encoding in ("", "identity"):
        return body, None
    if encoding == "gzip":
        return gzip.compress(body), "gzip"
    if encoding == "deflate":
        return zlib.compress(body), "deflate"
    if encoding == "compress":
        return compress_lzw(body), "compress"
    raise ValueError(f"unsupported fixture encoding {encoding}")


def text_body(role: str, content_type: str) -> bytes:
    _media, charset = split_content_type(content_type)
    return ROLES[role].encode(charset or "utf-8")


class FixtureOrigin:
    def __init__(self, origin: str, robots_text: str, robots_headers: dict[str, str] | None = None) -> None:
        self.origin = origin.rstrip("/")
        self.robots_text = robots_text
        self.robots_headers = dict(robots_headers or {"Content-Type": "text/plain"})
        self.robots_status = 200
        self.pages: dict[str, Page] = {}
        self.faults: dict[str, list[Fault]] = {}
        self.extra: dict[str, Response] = {}
        self.revisions: list[dict] = []
        self.changes: list[tuple[str, float]] = []
        self.request_log: list[dict] = []
        self.seed_path = "/catalog/"

    def add_page(self, page: Page) -> None:
        self.pages[page.path] = page

    def push_fault(self, path: str, fault: Fault) -> None:
        self.faults.setdefault(path, []).append(fault)

    def absolute(self, path: str) -> str:
        return self.origin + path

    def apply_horizon(self, horizon_index: int, now: float) -> None:
        for revision in self.revisions:
            if int(revision.get("horizon", -1)) != horizon_index:
                continue
            content_type = revision.get("content_type", "text/html; charset=utf-8")
            body = text_body(revision["role"], content_type)
            self.replace_body(
                revision["path"],
                body,
                now,
                etag=revision.get("etag"),
                encoding=revision.get("encoding", "identity"),
                content_type=content_type,
                weak=bool(revision.get("weak", False)),
            )

    def replace_body(
        self,
        path: str,
        body: bytes,
        now: float,
        *,
        etag: str | None = None,
        encoding: str = "identity",
        content_type: str | None = None,
        weak: bool = False,
    ) -> None:
        page = self.pages[path]
        if page.body != body:
            self.changes.append((self.absolute(path), now))
        page.body = body
        page.encoding = encoding
        page.weak = weak
        if etag is not None:
            page.etag = etag
        if content_type is not None:
            page.headers["Content-Type"] = content_type

    def handle(self, url: str, headers: dict[str, str], now: float) -> Response:
        self.request_log.append({"url": url, "headers": dict(headers), "now": now})
        if url in self.extra:
            extra = self.extra[url]
            return Response(extra.status, dict(extra.headers), extra.body, url)
        path = urlsplit(url).path or "/"
        queued = self.faults.get(path)
        if queued:
            fault = queued.pop(0)
            if fault.timeout:
                raise OriginTimeout(url)
            return Response(fault.status, dict(fault.headers), fault.body, url)
        if path == "/robots.txt":
            return self._robots(url)
        page = self.pages.get(path)
        if page is None:
            missing = b"not found"
            return Response(404, {"Content-Type": "text/plain"}, missing, url)
        return self._page(url, page, headers)

    def _robots(self, url: str) -> Response:
        body = self.robots_text.encode("utf-8")
        return Response(self.robots_status, dict(self.robots_headers), body, url)

    def _page(self, url: str, page: Page, headers: dict[str, str]) -> Response:
        lowered = {key.lower(): value for key, value in headers.items()}
        if page.status in (301, 302, 303, 307, 308):
            return Response(page.status, dict(page.headers), page.body, url)
        current = page.etag
        if page.weak and current:
            _opaque, _weak = split_etag(current)
            current = "W/" + (_opaque or current)
        matched = False
        if "if-none-match" in lowered:
            listed = parse_etag_list(lowered["if-none-match"])
            matched = any(item == "*" or etags_match(item, current) for item in listed)
            if matched:
                return self._not_modified(url, page)
            return self._representation(url, page)
        if "if-modified-since" in lowered and page.last_modified:
            if _not_modified_since(page.last_modified, lowered["if-modified-since"]):
                return self._not_modified(url, page)
        return self._representation(url, page)

    def _not_modified(self, url: str, page: Page) -> Response:
        headers: dict[str, str] = {}
        if page.etag:
            opaque, _weak = split_etag(page.etag)
            headers["ETag"] = ("W/" + opaque) if page.weak and opaque else (opaque or page.etag)
        if page.last_modified:
            headers["Last-Modified"] = page.last_modified
        return Response(304, headers, b"", url)

    def _representation(self, url: str, page: Page) -> Response:
        wire, coding = encode_wire(page.body, page.encoding)
        headers = dict(page.headers)
        if page.etag:
            opaque, _weak = split_etag(page.etag)
            headers["ETag"] = ("W/" + opaque) if page.weak and opaque else (opaque or page.etag)
        if page.last_modified:
            headers["Last-Modified"] = page.last_modified
        if coding:
            headers["Content-Encoding"] = coding
        return Response(page.status, headers, wire, url)

    def requests_for(self, path: str) -> list[dict]:
        return [item for item in self.request_log if urlsplit(item["url"]).path == path]


def _not_modified_since(last_modified: str, since: str) -> bool:
    left = _http_time(last_modified)
    right = _http_time(since)
    if left is None or right is None:
        return False
    return left <= right


def _http_time(value: str) -> float | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def load_site(path: str | Path) -> FixtureOrigin:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    robots = raw["robots"]
    origin = FixtureOrigin(
        raw["origin"],
        robots["body"],
        robots.get("headers"),
    )
    origin.robots_status = int(robots.get("status", 200))
    origin.seed_path = raw.get("seed_path", "/catalog/")
    origin.revisions = list(raw.get("revisions", []))
    for spec in raw["pages"]:
        content_type = spec.get("content_type", "text/html; charset=utf-8")
        if "role" in spec:
            body = text_body(spec["role"], content_type)
        else:
            body = spec.get("body", "").encode("utf-8")
        origin.add_page(
            Page(
                path=spec["path"],
                body=body,
                status=int(spec.get("status", 200)),
                headers={"Content-Type": content_type, **spec.get("headers", {})},
                encoding=spec.get("encoding", "identity"),
                etag=spec.get("etag"),
                weak=bool(spec.get("weak", False)),
                last_modified=spec.get("last_modified"),
            )
        )
    return origin
