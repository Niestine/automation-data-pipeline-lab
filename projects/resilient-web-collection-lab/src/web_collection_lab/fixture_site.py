"""Local HTML fixture site. Speaks HTTP request/response shapes in-process."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
import html as html_lib
import hashlib

from .errors import TransportError
from .seed import ROBOTS_TXT, build_catalog
from .transport import HttpRequest, HttpResponse


LAST_MODIFIED = "Thu, 01 Jan 2026 00:00:00 GMT"
PAGE_SIZE = 4


@dataclass
class Fault:
    method: str
    path: str
    status: Optional[int] = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: bool = False
    raw_body: Optional[bytes] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fault:
        if not isinstance(data, dict):
            raise ValueError("fault must be an object")
        if "method" not in data or "path" not in data:
            raise ValueError("fault requires method and path")
        headers = {str(k): str(v) for k, v in (data.get("headers") or {}).items()}
        status = data.get("status")
        if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
            raise ValueError("fault.status must be an integer")
        raw_body = data.get("raw_body")
        if raw_body is not None and not isinstance(raw_body, (str, bytes, bytearray)):
            raise ValueError("fault.raw_body must be a string or bytes")
        body: Optional[bytes]
        if isinstance(raw_body, str):
            body = raw_body.encode("utf-8")
        elif isinstance(raw_body, (bytes, bytearray)):
            body = bytes(raw_body)
        else:
            body = None
        return cls(
            method=str(data["method"]).upper(),
            path=str(data["path"]),
            status=status,
            headers=headers,
            timeout=bool(data.get("timeout")),
            raw_body=body,
        )


class FixtureSite:
    """Synthetic apparel catalog. Catalog rows are mutable so tests can inject pages."""

    def __init__(
        self,
        products: Optional[list[dict[str, Any]]] = None,
        *,
        robots_txt: str = ROBOTS_TXT,
        faults: Optional[list[Fault | dict[str, Any]]] = None,
        page_size: int = PAGE_SIZE,
        require_user_agent: bool = True,
    ) -> None:
        self.products = [dict(row) for row in (products if products is not None else build_catalog())]
        self.robots_txt = robots_txt
        self.page_size = max(1, int(page_size))
        self.require_user_agent = require_user_agent
        self.faults: list[Fault] = [
            item if isinstance(item, Fault) else Fault.from_dict(item) for item in (faults or [])
        ]
        self.call_log: list[dict[str, Any]] = []
        self._req_n = 0

    def handle(self, request: HttpRequest) -> HttpResponse:
        self._req_n += 1
        request_id = f"req_{self._req_n:04d}"
        self.call_log.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "user_agent": request.headers.get("user-agent"),
                "if_none_match": request.headers.get("if-none-match"),
                "request_id": request_id,
            }
        )
        if request.method != "GET":
            return HttpResponse(
                405,
                {"allow": "GET", "x-request-id": request_id},
                b"method not allowed",
            )
        if self.require_user_agent and not (request.headers.get("user-agent") or "").strip():
            return HttpResponse(400, {"x-request-id": request_id}, b"missing user-agent")

        fault = self._consume_fault(request)
        if fault is not None:
            if fault.timeout:
                raise TransportError("timeout", "fixture timeout")
            headers = {"x-request-id": request_id, **fault.headers}
            body = fault.raw_body if fault.raw_body is not None else b"fault"
            return HttpResponse(int(fault.status or 500), headers, body)

        path = request.path
        if path == "/robots.txt":
            return self._text(200, self.robots_txt.encode("utf-8"), "text/plain; charset=utf-8", request_id)
        if path == "/catalog":
            page = _page_number(request.query.get("page"))
            body, content_type = render_listing(self.products, page=page, page_size=self.page_size)
            return self._conditional(request, body, content_type, request_id)
        if path.startswith("/products/"):
            slug = path.rsplit("/", 1)[-1]
            if slug == "poison":
                body, content_type = render_poison()
                return self._conditional(request, body, content_type, request_id)
            row = self._by_slug(slug)
            if row is None:
                return HttpResponse(404, {"x-request-id": request_id}, b"not found")
            body, content_type = render_product(row)
            return self._conditional(request, body, content_type, request_id)
        if path == "/private/hidden":
            body, content_type = render_private()
            return self._conditional(request, body, content_type, request_id)
        if path.startswith("/images/"):
            return HttpResponse(
                200,
                {
                    "content-type": "image/jpeg",
                    "x-request-id": request_id,
                    "etag": _etag(b"stub-image"),
                },
                b"stub-image",
            )
        return HttpResponse(404, {"x-request-id": request_id}, b"not found")

    def _by_slug(self, slug: str) -> Optional[dict[str, Any]]:
        for row in self.products:
            if row.get("slug") == slug or str(row.get("sku", "")).lower() == slug.lower():
                return row
        return None

    def _consume_fault(self, request: HttpRequest) -> Optional[Fault]:
        for index, fault in enumerate(self.faults):
            if fault.method != request.method:
                continue
            if request.path != fault.path:
                continue
            return self.faults.pop(index)
        return None

    def _conditional(
        self,
        request: HttpRequest,
        body: bytes,
        content_type: str,
        request_id: str,
    ) -> HttpResponse:
        etag = _etag(body)
        headers = {
            "content-type": content_type,
            "etag": etag,
            "last-modified": LAST_MODIFIED,
            "x-request-id": request_id,
            "cache-control": "public, max-age=0",
        }
        if request.headers.get("if-none-match") == etag:
            return HttpResponse(304, headers, b"")
        return HttpResponse(200, headers, body)

    def _text(self, status: int, body: bytes, content_type: str, request_id: str) -> HttpResponse:
        return HttpResponse(
            status,
            {
                "content-type": content_type,
                "etag": _etag(body),
                "x-request-id": request_id,
            },
            body,
        )


def _etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest() + '"'


def _page_number(raw: Optional[str]) -> int:
    if raw is None or raw == "":
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _listing_entries(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = [dict(row) for row in products]
    entries.append({"kind": "poison"})
    entries.append({"kind": "private"})
    return entries


def render_listing(products: list[dict[str, Any]], *, page: int, page_size: int) -> tuple[bytes, str]:
    entries = _listing_entries(products)
    start = (page - 1) * page_size
    chunk = entries[start : start + page_size]
    has_next = start + page_size < len(entries)
    cards = "\n".join(_render_card(item) for item in chunk)
    next_link = ""
    if has_next:
        next_link = f'    <a rel="next" href="/catalog?page={page + 1}">Next</a>\n'
    document = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        "  <title>Northwind Apparel Catalog</title>\n"
        "</head>\n"
        "<body>\n"
        "  <h1>Catalog</h1>\n"
        '  <ul class="products">\n'
        f"{cards}\n"
        "  </ul>\n"
        "  <nav>\n"
        f"{next_link}"
        "  </nav>\n"
        "</body>\n"
        "</html>\n"
    )
    return document.encode("utf-8"), "text/html; charset=utf-8"


def _render_card(item: dict[str, Any]) -> str:
    if item.get("kind") == "poison":
        return (
            '    <li class="product-card">\n'
            '      <a class="product-link" href="/products/poison">Broken Card</a>\n'
            "    </li>"
        )
    if item.get("kind") == "private":
        return (
            '    <li class="product-card" data-sku="SKU-9999">\n'
            '      <a class="product-link" href="/private/hidden">Hidden Drop</a>\n'
            "    </li>"
        )
    sku = html_lib.escape(str(item["sku"]))
    href = html_lib.escape(f"/products/{item['slug']}")
    title = html_lib.escape(str(item.get("title_html") or item["title"]))
    price = html_lib.escape(str(item["price_text"]))
    currency = html_lib.escape(str(item["currency"]))
    return (
        f'    <li class="product-card" data-sku="{sku}">\n'
        f'      <a class="product-link" href="{href}">{title}</a>\n'
        f'      <span class="price" data-currency="{currency}">{price}</span>\n'
        "    </li>"
    )


def render_product(row: dict[str, Any]) -> tuple[bytes, str]:
    charset = str(row.get("charset") or "utf-8")
    sku = html_lib.escape(str(row["sku"]))
    availability = html_lib.escape(str(row["availability"]))
    color = html_lib.escape(str(row["color"]))
    size = html_lib.escape(str(row["size"]))
    title = html_lib.escape(str(row.get("title_html") or row["title"]))
    price = html_lib.escape(str(row["price_text"]))
    currency = html_lib.escape(str(row["currency"]))
    image = html_lib.escape(str(row["image"]))
    description = str(row.get("description_html") or html_lib.escape(str(row.get("description") or "")))
    cents_attr = ""
    if row.get("data_cents") is not None:
        cents_attr = f' data-cents="{int(row["data_cents"])}"'
    document = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        f'  <meta charset="{html_lib.escape(charset)}">\n'
        f"  <title>{html_lib.escape(str(row['title']))}</title>\n"
        "</head>\n"
        "<body>\n"
        f'  <article class="product" data-sku="{sku}" data-availability="{availability}" '
        f'data-color="{color}" data-size="{size}">\n'
        f'    <h1 class="title">{title}</h1>\n'
        f'    <p class="price" data-currency="{currency}"{cents_attr}>{price}</p>\n'
        f'    <img class="product-image" src="{image}" alt="">\n'
        f'    <p class="description">{description}</p>\n'
        "  </article>\n"
        "</body>\n"
        "</html>\n"
    )
    try:
        body = document.encode(charset)
    except (LookupError, UnicodeEncodeError):
        body = document.encode("utf-8")
        charset = "utf-8"
    return body, f"text/html; charset={charset}"


def render_poison() -> tuple[bytes, str]:
    document = (
        "<!DOCTYPE html>\n"
        "<html><head><meta charset=\"utf-8\"><title>Broken</title></head>\n"
        "<body>\n"
        '  <article class="product" data-sku="SKU-POISON" data-availability="in_stock" data-color="x" data-size="M">\n'
        '    <h1 class="title">Broken Price</h1>\n'
        '    <p class="price" data-currency="USD" data-cents="1">$29.00</p>\n'
        '    <img class="product-image" src="/images/poison.jpg" alt="">\n'
        '    <p class="description">intentionally invalid</p>\n'
        "  </article>\n"
        "</body></html>\n"
    )
    return document.encode("utf-8"), "text/html; charset=utf-8"


def render_private() -> tuple[bytes, str]:
    row = {
        "sku": "SKU-9999",
        "title": "Hidden Drop",
        "title_html": "Hidden Drop",
        "price_text": "$999.00",
        "price_cents": 99900,
        "currency": "USD",
        "availability": "in_stock",
        "color": "Black",
        "size": "m",
        "image": "/images/sku-9999.jpg",
        "description": "Should never be collected.",
        "description_html": "Should never be collected.",
        "charset": "utf-8",
        "data_cents": 99900,
    }
    return render_product(row)


