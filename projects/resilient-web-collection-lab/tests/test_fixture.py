import unittest

import helpers  # noqa: F401

from web_collection_lab.errors import TransportError
from web_collection_lab.fixture_site import FixtureSite
from web_collection_lab.models import USER_AGENT
from web_collection_lab.seed import build_catalog
from web_collection_lab.transport import HttpRequest


class FixtureTests(unittest.TestCase):
    def test_missing_user_agent_is_400(self):
        site = FixtureSite(build_catalog())
        response = site.handle(HttpRequest("GET", "/catalog"))
        self.assertEqual(response.status, 400)

    def test_robots_and_catalog_and_product(self):
        site = FixtureSite(build_catalog())
        headers = {"user-agent": USER_AGENT}
        robots = site.handle(HttpRequest("GET", "/robots.txt", headers=headers))
        self.assertEqual(robots.status, 200)
        self.assertIn(b"Disallow: /private/", robots.body)
        listing = site.handle(HttpRequest("GET", "/catalog", headers=headers))
        self.assertEqual(listing.status, 200)
        self.assertIn(b'rel="next"', listing.body)
        product = site.handle(HttpRequest("GET", "/products/sku-1001", headers=headers))
        self.assertEqual(product.status, 200)
        self.assertTrue(product.etag)

    def test_conditional_get_returns_304(self):
        site = FixtureSite(build_catalog())
        headers = {"user-agent": USER_AGENT}
        first = site.handle(HttpRequest("GET", "/products/sku-1002", headers=headers))
        second = site.handle(
            HttpRequest(
                "GET",
                "/products/sku-1002",
                headers={**headers, "if-none-match": first.etag},
            )
        )
        self.assertEqual(second.status, 304)
        self.assertEqual(second.body, b"")
        self.assertEqual(second.etag, first.etag)

    def test_one_shot_timeout_fault(self):
        site = FixtureSite(
            build_catalog(),
            faults=[{"method": "GET", "path": "/products/sku-1001", "timeout": True}],
        )
        headers = {"user-agent": USER_AGENT}
        with self.assertRaises(TransportError):
            site.handle(HttpRequest("GET", "/products/sku-1001", headers=headers))
        ok = site.handle(HttpRequest("GET", "/products/sku-1001", headers=headers))
        self.assertEqual(ok.status, 200)

    def test_private_page_exists_but_is_not_secret_to_the_fixture(self):
        site = FixtureSite(build_catalog())
        response = site.handle(HttpRequest("GET", "/private/hidden", headers={"user-agent": USER_AGENT}))
        self.assertEqual(response.status, 200)
        self.assertIn(b"SKU-9999", response.body)

    def test_unknown_product_is_404(self):
        site = FixtureSite(build_catalog())
        response = site.handle(HttpRequest("GET", "/products/missing", headers={"user-agent": USER_AGENT}))
        self.assertEqual(response.status, 404)

    def test_post_is_405(self):
        site = FixtureSite(build_catalog())
        response = site.handle(HttpRequest("POST", "/catalog", headers={"user-agent": USER_AGENT}))
        self.assertEqual(response.status, 405)


if __name__ == "__main__":
    unittest.main()
