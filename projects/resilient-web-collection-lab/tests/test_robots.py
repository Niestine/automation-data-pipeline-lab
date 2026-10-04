import unittest

import helpers  # noqa: F401

from web_collection_lab.models import MAX_CRAWL_DELAY_MS, USER_AGENT
from web_collection_lab.robots import parse_robots, product_token
from web_collection_lab.seed import ROBOTS_TXT


class RobotsTests(unittest.TestCase):
    def test_product_token_strips_version_and_url(self):
        self.assertEqual(product_token(USER_AGENT), "LabCollector")

    def test_lab_collector_group_wins_over_star(self):
        policy = parse_robots(ROBOTS_TXT, USER_AGENT)
        self.assertTrue(policy.allowed("/catalog"))
        self.assertTrue(policy.allowed("/products/sku-1001"))
        self.assertTrue(policy.allowed("/images/sku-1001.jpg"))
        self.assertFalse(policy.allowed("/private/hidden"))
        self.assertEqual(policy.crawl_delay_ms, 1000)

    def test_wildcard_group_used_for_unknown_agent(self):
        policy = parse_robots(ROBOTS_TXT, "OtherBot/9.0")
        self.assertFalse(policy.allowed("/admin/secret"))
        self.assertTrue(policy.allowed("/private/hidden"))
        self.assertEqual(policy.crawl_delay_ms, 0)

    def test_longest_allow_beats_shorter_disallow(self):
        text = (
            "User-agent: LabCollector\n"
            "Disallow: /private\n"
            "Allow: /private/ok\n"
        )
        policy = parse_robots(text, USER_AGENT)
        self.assertFalse(policy.allowed("/private/hidden"))
        self.assertTrue(policy.allowed("/private/ok"))
        self.assertTrue(policy.allowed("/private/ok/item"))

    def test_empty_disallow_allows_all(self):
        text = "User-agent: *\nDisallow:\n"
        policy = parse_robots(text, "Anything/1.0")
        self.assertTrue(policy.allowed("/anything"))

    def test_disallow_slash_blocks_everything_unless_longer_allow(self):
        text = "User-agent: *\nDisallow: /\nAllow: /catalog\n"
        policy = parse_robots(text, "x")
        self.assertFalse(policy.allowed("/products/sku-1001"))
        self.assertTrue(policy.allowed("/catalog"))
        self.assertTrue(policy.allowed("/catalog?page=2"))

    def test_privateer_is_not_blocked_by_private_prefix(self):
        text = "User-agent: *\nDisallow: /private\n"
        policy = parse_robots(text, "x")
        self.assertTrue(policy.allowed("/privateer"))
        self.assertFalse(policy.allowed("/private"))
        self.assertFalse(policy.allowed("/private/hidden"))

    def test_crawl_delay_is_capped(self):
        text = "User-agent: LabCollector\nCrawl-delay: 86400\nDisallow: /private/\n"
        policy = parse_robots(text, USER_AGENT)
        self.assertEqual(policy.crawl_delay_ms, MAX_CRAWL_DELAY_MS)

    def test_comments_and_unknown_fields_are_ignored(self):
        text = (
            "# comment\n"
            "User-agent: LabCollector\n"
            "Sitemap: https://fixture.example.invalid/sitemap.xml\n"
            "Disallow: /private/\n"
        )
        policy = parse_robots(text, USER_AGENT)
        self.assertFalse(policy.allowed("/private/x"))

    def test_group_match_is_exact_not_prefix(self):
        text = (
            "User-agent: LabCollectorX\n"
            "Disallow: /\n"
            "\n"
            "User-agent: L\n"
            "Disallow: /\n"
            "\n"
            "User-agent: *\n"
            "Disallow: /admin/\n"
        )
        policy = parse_robots(text, USER_AGENT)
        self.assertEqual(policy.matched_group.agents, ["*"])
        self.assertTrue(policy.allowed("/catalog"))
        self.assertFalse(policy.allowed("/admin/x"))

    def test_groups_for_the_same_agent_are_merged(self):
        text = (
            "User-agent: labcollector\n"
            "Disallow: /private/\n"
            "\n"
            "User-agent: *\n"
            "Disallow: /\n"
            "\n"
            "User-agent: LabCollector\n"
            "Disallow: /drafts/\n"
            "Crawl-delay: 2\n"
        )
        policy = parse_robots(text, USER_AGENT)
        self.assertFalse(policy.allowed("/private/a"))
        self.assertFalse(policy.allowed("/drafts/b"))
        self.assertTrue(policy.allowed("/catalog"))
        self.assertEqual(policy.crawl_delay_ms, 2000)

    def test_missing_robots_allows_all(self):
        policy = parse_robots("", USER_AGENT)
        self.assertTrue(policy.allowed("/private/hidden"))
        self.assertEqual(policy.crawl_delay_ms, 0)


if __name__ == "__main__":
    unittest.main()
