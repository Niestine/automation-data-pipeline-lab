"""In-process client. Conditional headers follow the stored validator.

A stored ETag, weak or strong, is sent as If-None-Match and If-Modified-Since
is omitted. Last-Modified is sent only when no ETag is stored. Changed Accept
or Accept-Language drops the validator and fetches unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass

from incremental_crawl_lab.config import Config
from incremental_crawl_lab.observe import format_etag


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


def conditional_headers(row: dict | None, config: Config) -> dict[str, str]:
    headers = {
        "User-Agent": config.user_agent,
        "Accept": config.accept,
        "Accept-Language": config.accept_language,
    }
    if not row or not row.get("observation_count"):
        return headers
    if row.get("variant_accept") not in (None, config.accept):
        return headers
    if row.get("variant_lang") not in (None, config.accept_language):
        return headers
    if row.get("etag"):
        headers["If-None-Match"] = format_etag(str(row["etag"]), bool(row.get("weak")))
        return headers
    if row.get("last_modified"):
        headers["If-Modified-Since"] = str(row["last_modified"])
    return headers


class FixtureClient:
    """Call the local origin. No socket is opened."""

    def __init__(self, origin) -> None:
        self.origin = origin

    def request(self, url: str, headers: dict[str, str], now: float) -> Response:
        return self.origin.handle(url, headers, now)
