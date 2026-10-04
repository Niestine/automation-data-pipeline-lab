"""HTML listing and product parsers built on html.parser.HTMLParser."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
import re

from .errors import ParseError


@dataclass(frozen=True)
class ListingCard:
    href: str
    sku: str
    title: str


@dataclass(frozen=True)
class ListingPage:
    cards: tuple[ListingCard, ...]
    next_href: Optional[str]


@dataclass(frozen=True)
class ParsedProduct:
    sku: str
    title: str
    price_text: str
    currency: str
    data_cents: Optional[int]
    availability: str
    color: str
    size: str
    image: str
    description: str


def parse_listing(html: str) -> ListingPage:
    parser = _CollectionHTMLParser()
    parser.feed(html)
    parser.close()
    cards = tuple(ListingCard(href=c["href"], sku=c.get("sku") or "", title=c.get("title") or "") for c in parser.cards)
    return ListingPage(cards=cards, next_href=parser.next_href)


def parse_product(html: str) -> ParsedProduct:
    parser = _CollectionHTMLParser()
    parser.feed(html)
    parser.close()
    data = parser.product
    if not data:
        raise ParseError("product page is missing <article class=\"product\">")
    data_cents = None
    raw_cents = data.get("data_cents")
    if raw_cents not in (None, ""):
        # int() would also accept "+5", "1_000", or non-ASCII digits.
        if re.fullmatch(r"[0-9]{1,12}", raw_cents.strip()) is None:
            raise ParseError(f"invalid data-cents {raw_cents!r}", field="price")
        data_cents = int(raw_cents.strip())
    return ParsedProduct(
        sku=data.get("sku") or "",
        title=data.get("title") or "",
        price_text=data.get("price") or "",
        currency=data.get("currency") or "",
        data_cents=data_cents,
        availability=data.get("availability") or "",
        color=data.get("color") or "",
        size=data.get("size") or "",
        image=data.get("image") or "",
        description=data.get("description") or "",
    )


class _CollectionHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self.product: dict[str, str] = {}
        self.next_href: Optional[str] = None
        self._card: Optional[dict[str, str]] = None
        self._in_product = False
        self._text_target: Optional[str] = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        ad = {str(key).lower(): (value or "") for key, value in attrs}
        class_set = set((ad.get("class") or "").split())
        rel_set = set((ad.get("rel") or "").split())

        if tag == "li" and "product-card" in class_set:
            self._flush_text()
            self._card = {
                "sku": ad.get("data-sku") or "",
                "href": "",
                "title": "",
            }
        if self._card is not None and tag == "a" and "product-link" in class_set:
            self._card["href"] = ad.get("href") or ""
            self._begin("card_title")
        if tag == "a" and "next" in rel_set:
            self.next_href = ad.get("href") or None

        if tag == "article" and "product" in class_set:
            self._flush_text()
            self._in_product = True
            self.product = {
                "sku": ad.get("data-sku") or "",
                "availability": ad.get("data-availability") or "",
                "color": ad.get("data-color") or "",
                "size": ad.get("data-size") or "",
            }
        if not self._in_product:
            return
        if tag in {"h1", "h2"} and "title" in class_set:
            self._begin("title")
        elif tag in {"p", "span"} and "price" in class_set:
            self.product["currency"] = ad.get("data-currency") or ""
            if "data-cents" in ad:
                self.product["data_cents"] = ad["data-cents"]
            self._begin("price")
        elif tag == "img" and "product-image" in class_set:
            self.product["image"] = ad.get("src") or ad.get("data-src") or ""
        elif tag == "p" and "description" in class_set:
            self._begin("description")

    def handle_endtag(self, tag: str) -> None:
        self._flush_text()
        if tag == "li" and self._card is not None:
            self.cards.append(self._card)
            self._card = None
        if tag == "article" and self._in_product:
            self._in_product = False

    def handle_data(self, data: str) -> None:
        if self._text_target is not None:
            self._text.append(data)

    def _begin(self, target: str) -> None:
        self._flush_text()
        self._text_target = target
        self._text = []

    def _flush_text(self) -> None:
        if self._text_target is None:
            return
        text = "".join(self._text)
        if self._text_target == "card_title" and self._card is not None:
            self._card["title"] = text
        elif self._in_product:
            self.product[self._text_target] = text
        self._text_target = None
        self._text = []
