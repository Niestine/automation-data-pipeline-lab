import helpers  # noqa: F401
import unittest

from incremental_crawl_lab.canon import canonicalize, extract_hrefs, is_allowed_host, same_origin


class CanonTest(unittest.TestCase):
    def test_scheme_host_port_and_fragment(self) -> None:
        self.assertEqual(
            canonicalize("HTTPS://Catalog.Example.Invalid:443/catalog/a?b=1#top"),
            "https://catalog.example.invalid/catalog/a?b=1",
        )
        self.assertEqual(canonicalize("http://Catalog.Example.Invalid:80"), "http://catalog.example.invalid/")
        self.assertEqual(
            canonicalize("https://catalog.example.invalid:8443/x"),
            "https://catalog.example.invalid:8443/x",
        )
        self.assertEqual(
            canonicalize("https://user:pw@catalog.example.invalid/x"),
            "https://catalog.example.invalid/x",
        )

    def test_relative_links_resolve_against_the_page(self) -> None:
        base = "https://catalog.example.invalid/catalog/coats/wool"
        self.assertEqual(canonicalize("linen", base), "https://catalog.example.invalid/catalog/coats/linen")
        self.assertEqual(canonicalize("../shirts/", base), "https://catalog.example.invalid/catalog/shirts/")
        self.assertEqual(canonicalize("/catalog/", base), "https://catalog.example.invalid/catalog/")
        self.assertEqual(canonicalize("#reviews", base), base)

    def test_unusable_links_have_no_key(self) -> None:
        self.assertEqual(canonicalize("mailto:desk@catalog.example.invalid"), "")
        self.assertEqual(canonicalize("javascript:void(0)"), "")
        self.assertEqual(canonicalize("http://catalog.example.invalid:abc/x"), "")

    def test_origin_and_allowlist_checks(self) -> None:
        origin = "https://catalog.example.invalid"
        allow = ("catalog.example.invalid",)
        self.assertTrue(same_origin("https://catalog.example.invalid/catalog/a", origin))
        self.assertFalse(same_origin("http://catalog.example.invalid/catalog/a", origin))
        self.assertFalse(same_origin("https://catalog.example.invalid:8443/a", origin))
        self.assertFalse(is_allowed_host("https://catalog.example.invalid.evil.example/", allow))
        self.assertEqual(
            extract_hrefs('<a href="/a">a</a><A HREF="b">b</A><a name="x">x</a>'),
            ["/a", "b"],
        )


if __name__ == "__main__":
    unittest.main()
