"""Canonical URLs for the allowlisted collection."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, urljoin


def canonicalize(url: str, base: str | None = None) -> str:
    """Lowercase the scheme and host, drop the fragment and userinfo, strip default ports."""
    try:
        joined = urljoin(base, url) if base else url
        parts = urlsplit(joined)
        port = parts.port
    except ValueError:
        # A malformed authority, such as a non-numeric port, has no canonical key.
        return ""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        return ""
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def path_and_query(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        return path + "?" + parts.query
    return path


def same_origin(url: str, origin: str) -> bool:
    return origin_of(url) == origin.rstrip("/")


def is_allowed_host(url: str, allowlist: tuple[str, ...]) -> bool:
    return host_of(url) in allowlist


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)


def extract_hrefs(html: str) -> list[str]:
    parser = _HrefParser()
    parser.feed(html)
    parser.close()
    return parser.hrefs
