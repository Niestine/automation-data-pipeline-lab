import helpers  # noqa: F401
import unittest

from incremental_crawl_lab.clock import demo_start_seconds
from incremental_crawl_lab.robots import (
    cache_lifetime,
    is_allowed,
    parse_robots,
    rule_matches,
    select_rules,
)


class RobotsProtocolTest(unittest.TestCase):
    def test_longest_match_and_end_anchor(self) -> None:
        text = """User-agent: *
Disallow: *.gif$
Disallow: /example/
Allow: /publications/
"""
        rules = select_rules(parse_robots(text), "FrontierBot")
        cases = {
            "/publications/paper": True,
            "/example/secret": False,
            "/img/cat.gif": False,
            "/publications/fig.gif": True,
            "/example/page.gif": False,
            "/cat.gif": False,
        }
        for path, allowed in cases.items():
            self.assertEqual(is_allowed(path, rules), allowed, path)

    def test_rfc9309_simple_example_groups(self) -> None:
        text = """User-Agent: *
Disallow: *.gif$
Disallow: /example/
Allow: /publications/

User-Agent: foobot
Disallow:/
Allow:/example/page.html
Allow:/example/allowed.gif

User-Agent: barbot
User-Agent: bazbot
Disallow: /example/page.html

User-Agent: quxbot

EOF
"""
        groups = parse_robots(text)
        foobot = select_rules(groups, "foobot")
        self.assertTrue(is_allowed("/example/page.html", foobot))
        self.assertTrue(is_allowed("/example/allowed.gif", foobot))
        self.assertFalse(is_allowed("/example/other", foobot))
        self.assertFalse(is_allowed("/publications/", foobot))
        for token in ("barbot", "bazbot"):
            rules = select_rules(groups, token)
            self.assertFalse(is_allowed("/example/page.html", rules))
            self.assertTrue(is_allowed("/example/other", rules))
            self.assertTrue(is_allowed("/img/cat.gif", rules))
        quxbot = select_rules(groups, "quxbot")
        self.assertTrue(is_allowed("/example/page.html", quxbot))
        self.assertTrue(is_allowed("/img/cat.gif", quxbot))

    def test_rfc9309_longest_match_example(self) -> None:
        text = """User-Agent: foobot
Allow: /example/page/
Disallow: /example/page/disallowed.gif
"""
        rules = select_rules(parse_robots(text), "foobot")
        self.assertFalse(is_allowed("/example/page/disallowed.gif", rules))
        self.assertTrue(is_allowed("/example/page/other.gif", rules))

    def test_equal_length_allow_wins(self) -> None:
        rules = [("disallow", "/a"), ("allow", "/a")]
        self.assertTrue(is_allowed("/a", rules))

    def test_specific_group_is_not_merged_with_star(self) -> None:
        text = """User-agent: FrontierBot
Allow: /catalog/
Disallow: /private/

User-agent: *
Disallow: /catalog/secret
"""
        rules = select_rules(parse_robots(text), "FrontierBot")
        self.assertTrue(is_allowed("/catalog/secret", rules))
        self.assertFalse(is_allowed("/private/cost", rules))
        self.assertFalse(rule_matches("/private/", "/privateer"))
        self.assertTrue(rule_matches("/private/", "/private/cost"))
        # Path matching is case-sensitive.
        self.assertTrue(is_allowed("/Private/cost", rules))

    def test_star_is_only_the_fallback(self) -> None:
        text = """User-agent: Other
Disallow: /catalog/

User-agent: *
Disallow: /only-star
"""
        rules = select_rules(parse_robots(text), "frontierbot")
        self.assertFalse(is_allowed("/only-star", rules))
        self.assertTrue(is_allowed("/catalog/", rules))
        other = select_rules(parse_robots(text), "FrontierBotX")
        self.assertFalse(is_allowed("/only-star", other))

    def test_empty_pattern_and_rules_before_any_agent_are_ignored(self) -> None:
        text = """Disallow: /early
User-agent: FrontierBot
Disallow:
Allow:
Allow: /catalog/
"""
        rules = select_rules(parse_robots(text), "FrontierBot")
        self.assertTrue(is_allowed("/early", rules))
        self.assertTrue(is_allowed("/catalog/item", rules))

    def test_crawl_delay_and_sitemap_do_not_end_the_group(self) -> None:
        text = """User-agent: FrontierBot
Disallow: /secret
Crawl-delay: 100
Allow: /secret/public
Sitemap: https://catalog.example.invalid/sitemap.xml
"""
        groups = parse_robots(text)
        self.assertEqual(len(groups), 1)
        rules = select_rules(groups, "FrontierBot")
        self.assertTrue(is_allowed("/secret/public", rules))
        self.assertFalse(is_allowed("/secret", rules))

    def test_percent_encoded_unreserved_octet(self) -> None:
        decoded = [("disallow", "/"), ("allow", "/foo/~bar")]
        self.assertTrue(is_allowed("/foo/%7Ebar", decoded))
        # %2F is reserved, so it stays encoded and does not match a decoded slash.
        self.assertTrue(is_allowed("/foo/%2Fbar", [("disallow", "/foo//bar")]))
        self.assertFalse(is_allowed("/foo/%2Fbar", [("disallow", "/foo/%2Fbar")]))

    def test_cache_lifetime_prefers_max_age(self) -> None:
        now = demo_start_seconds()
        headers = {
            "Cache-Control": "max-age=10",
            "Expires": "Tue, 06 Oct 2026 00:00:00 GMT",
        }
        self.assertEqual(cache_lifetime(headers, now, 86400), 10.0)
        self.assertEqual(cache_lifetime({"Cache-Control": "no-cache"}, now, 86400), 0.0)
        self.assertEqual(cache_lifetime({}, now, 86400), 86400.0)
        # An Expires value that is not an HTTP-date means already expired.
        self.assertEqual(cache_lifetime({"Expires": "0"}, now, 86400), 0.0)


if __name__ == "__main__":
    unittest.main()
