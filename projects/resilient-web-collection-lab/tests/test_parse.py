import unittest

import helpers  # noqa: F401

from web_collection_lab.decode import decode_html
from web_collection_lab.errors import ParseError
from web_collection_lab.fixture_site import render_listing, render_poison, render_product
from web_collection_lab.parse import parse_listing, parse_product
from web_collection_lab.seed import build_catalog


class ParseTests(unittest.TestCase):
    def test_listing_extracts_cards_and_next_link(self):
        products = build_catalog()
        body, _ctype = render_listing(products, page=1, page_size=4)
        html, _enc = decode_html(body, "text/html; charset=utf-8")
        listing = parse_listing(html)
        self.assertEqual(len(listing.cards), 4)
        self.assertEqual(listing.cards[0].href, "/products/sku-1001")
        self.assertEqual(listing.cards[0].sku, "SKU-1001")
        self.assertEqual(listing.next_href, "/catalog?page=2")

    def test_last_listing_page_has_poison_private_and_no_next(self):
        body, _ctype = render_listing(build_catalog(), page=2, page_size=4)
        html, _enc = decode_html(body, "text/html; charset=utf-8")
        listing = parse_listing(html)
        hrefs = [card.href for card in listing.cards]
        self.assertEqual(hrefs, ["/products/sku-1005", "/products/sku-1006", "/products/poison", "/private/hidden"])
        self.assertIsNone(listing.next_href)

    def test_product_unescapes_entities_and_keeps_nbsp_as_text(self):
        row = next(item for item in build_catalog() if item["sku"] == "SKU-1001")
        body, ctype = render_product(row)
        html, _enc = decode_html(body, ctype)
        parsed = parse_product(html)
        self.assertEqual(parsed.sku, "SKU-1001")
        self.assertIn("Linen", parsed.title)
        self.assertEqual(parsed.price_text.strip(), "$29.00")
        self.assertEqual(parsed.currency, "USD")
        self.assertEqual(parsed.data_cents, 2900)
        self.assertEqual(parsed.image, "/images/sku-1001.jpg")
        self.assertIn("&", parsed.description)
        self.assertIn("Machine wash", parsed.description)

    def test_latin1_cafe_title_survives_decode_and_parse(self):
        row = next(item for item in build_catalog() if item["sku"] == "SKU-1003")
        body, ctype = render_product(row)
        self.assertIn("iso-8859-1", ctype)
        self.assertIn(b"Caf\xe9", body)
        html, charset = decode_html(body, ctype)
        self.assertEqual(charset, "iso-8859-1")
        parsed = parse_product(html)
        self.assertIn("Café", parsed.title)
        self.assertEqual(parsed.currency, "EUR")
        self.assertIn(",", parsed.price_text)

    def test_poison_page_parses_but_keeps_mismatching_cents(self):
        body, ctype = render_poison()
        html, _enc = decode_html(body, ctype)
        parsed = parse_product(html)
        self.assertEqual(parsed.sku, "SKU-POISON")
        self.assertEqual(parsed.data_cents, 1)
        self.assertEqual(parsed.price_text.strip(), "$29.00")

    def test_non_ascii_or_signed_data_cents_is_a_parse_error(self):
        for raw in ("+5", "1_000", "١٢", "-1"):
            with self.subTest(raw=raw):
                html = (
                    '<article class="product" data-sku="SKU-1001">'
                    f'<p class="price" data-currency="USD" data-cents="{raw}">$1.00</p>'
                    "</article>"
                )
                with self.assertRaises(ParseError):
                    parse_product(html)

    def test_missing_article_raises(self):
        with self.assertRaises(ParseError):
            parse_product("<html><body><p>nope</p></body></html>")


if __name__ == "__main__":
    unittest.main()
