import helpers  # noqa: F401
import tempfile
import unittest
from pathlib import Path

from incremental_crawl_lab.config import Config
from incremental_crawl_lab.crawl import Crawler
from incremental_crawl_lab.errors import SimulatedCrash
from incremental_crawl_lab.fixture import Page
from incremental_crawl_lab.store import Store

ALLOW = "User-agent: FrontierBot\nAllow: /\n"


class CheckpointTest(unittest.TestCase):
    def test_crash_before_send_retries_once_without_a_second_charge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "crawl.sqlite")
            page = Page(
                "/catalog/item",
                b"<html><body><p>item</p></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
                etag='"item-1"',
            )
            crawler, origin, store, _clock = helpers.make_lab(
                [page], ALLOW, db_path=path, seed_path="/catalog/item"
            )
            crawler.crash_point = "pre_request"
            with self.assertRaises(SimulatedCrash):
                crawler.run()
            self.assertEqual(origin.requests_for("/catalog/item"), [])
            store.close()
            reopened = Store.open(path, durable=True)
            url = origin.absolute("/catalog/item")
            self.assertEqual(reopened.get(url)["status"], "in_flight")
            self.assertEqual(reopened.run_state()["page_budget_left"], Config().page_budget)
            nxt = reopened.host_next(Config().origin)
            resumed = Crawler(Config(), reopened, origin)
            resumed.run()
            self.assertGreaterEqual(resumed.request_starts[0], nxt)
            self.assertEqual(reopened.get(url)["status"], "live")
            self.assertEqual(reopened.run_state()["page_budget_left"], Config().page_budget - 1)
            self.assertEqual(len(origin.requests_for("/catalog/item")), 1)
            self.assertEqual(
                [event["kind"] for event in reopened.events()].count("retry"),
                0,
            )
            reopened.close()

    def test_crash_after_commit_does_not_refetch_or_refill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "crawl.sqlite")
            page = Page(
                "/catalog/item",
                b"<html><body><p>item</p></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
                etag='"item-1"',
            )
            crawler, origin, store, _clock = helpers.make_lab(
                [page], ALLOW, db_path=path, seed_path="/catalog/item"
            )
            crawler.crash_point = "post_result"
            with self.assertRaises(SimulatedCrash):
                crawler.run()
            store.close()
            reopened = Store.open(path, durable=True)
            url = origin.absolute("/catalog/item")
            self.assertEqual(reopened.get(url)["status"], "live")
            left = reopened.run_state()["page_budget_left"]
            self.assertEqual(left, Config().page_budget - 1)
            resumed = Crawler(Config(), reopened, origin)
            resumed.run()
            self.assertEqual(reopened.run_state()["page_budget_left"], left)
            self.assertEqual(len(origin.requests_for("/catalog/item")), 1)
            self.assertEqual(reopened.get(url)["status"], "live")
            reopened.close()

    def test_apply_charges_the_page_budget_once(self) -> None:
        store = Store.open(None, durable=False)
        config = Config()
        store.begin_horizon(0.0, config)
        url = "https://catalog.example.invalid/catalog/item"
        store.admit(url, origin=config.origin, depth=0, now=0.0, capacity=10, protected=set())
        store.arm_request(config.origin, url, 0.0, 10.0)
        from incremental_crawl_lab.corpus import BASE
        from incremental_crawl_lab.observe import interpret

        decision = interpret(
            {},
            200,
            {"Content-Type": "text/html; charset=utf-8", "ETag": '"v"'},
            BASE.encode("utf-8"),
            config,
        )
        self.assertTrue(store.apply_decision(url, decision, 1.0, sample_interval=100.0))
        self.assertFalse(store.apply_decision(url, decision, 2.0, sample_interval=100.0))
        self.assertEqual(store.run_state()["page_budget_left"], config.page_budget - 1)
        store.close()


if __name__ == "__main__":
    unittest.main()
