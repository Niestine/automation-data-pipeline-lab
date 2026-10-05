import helpers  # noqa: F401
import json
import unittest

from incremental_crawl_lab.client import Response
from incremental_crawl_lab.config import Config, load_config
from incremental_crawl_lab.corpus import AD_EDIT, BASE, LINEN, PARAGRAPH_EDIT
from incremental_crawl_lab.fingerprint import format_simhash, sha256_hex, simhash_text
from incremental_crawl_lab.fixture import Fault, Page, load_site
from incremental_crawl_lab.report import build_report

ALLOW = "User-agent: FrontierBot\nAllow: /\n"


def _html(text: str) -> bytes:
    return f"<html><body><p>{text}</p></body></html>".encode("utf-8")


class CrawlBehaviorTest(unittest.TestCase):
    def test_same_host_gap_includes_a_retry(self) -> None:
        pages = [
            Page("/catalog/a", _html("alpha"), headers={"Content-Type": "text/html; charset=utf-8"}, etag='"a"'),
            Page("/catalog/b", _html("beta"), headers={"Content-Type": "text/html; charset=utf-8"}, etag='"b"'),
        ]
        crawler, origin, store, _clock = helpers.make_lab(pages, ALLOW, seed_path="/catalog/a")
        origin.push_fault("/catalog/a", Fault(timeout=True))
        for path, depth in (("/catalog/a", 0), ("/catalog/b", 1)):
            store.admit(
                origin.absolute(path),
                origin=crawler.config.origin,
                depth=depth,
                now=0.0,
                capacity=10,
                protected=set(),
            )
        crawler.run()
        starts = crawler.request_starts
        self.assertGreaterEqual(len(starts), 4)
        gaps = [right - left for left, right in zip(starts, starts[1:])]
        self.assertTrue(all(gap >= 10.0 for gap in gaps))
        self.assertEqual(len(origin.requests_for("/catalog/a")), 2)
        store.close()

    def test_long_retry_after_defers_without_sleeping_it(self) -> None:
        page = Page("/catalog/a", _html("alpha"), headers={"Content-Type": "text/html; charset=utf-8"})
        crawler, origin, store, clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
        origin.push_fault(
            "/catalog/a",
            Fault(status=429, headers={"Retry-After": "10000"}, body=b"slow"),
        )
        start = clock.now
        crawler.run()
        self.assertLess(clock.now - start, 3600.0)
        self.assertEqual(store.run_state()["page_budget_left"], crawler.config.page_budget)
        self.assertTrue(any(event["kind"] == "deferred" for event in store.events()))
        self.assertEqual(len(origin.requests_for("/catalog/a")), 1)
        store.close()

    def test_http_date_beyond_an_hour_also_defers(self) -> None:
        page = Page("/catalog/a", _html("alpha"), headers={"Content-Type": "text/html; charset=utf-8"})
        crawler, origin, store, clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
        origin.push_fault(
            "/catalog/a",
            Fault(status=503, headers={"Retry-After": "Tue, 06 Oct 2026 00:00:00 GMT"}),
        )
        start = clock.now
        crawler.run()
        self.assertLess(clock.now - start, 3600.0)
        self.assertEqual(store.get(origin.absolute("/catalog/a"))["status"] != "live", True)
        store.close()

    def test_retryable_status_does_not_spend_the_page_budget(self) -> None:
        page = Page(
            "/catalog/a",
            _html("alpha"),
            headers={"Content-Type": "text/html; charset=utf-8", "ETag": '"a"'},
            etag='"a"',
        )
        for status in (503, 431):
            crawler, origin, store, _clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
            origin.push_fault("/catalog/a", Fault(status=status, headers={"Retry-After": "30"}))
            crawler.run()
            row = store.get(origin.absolute("/catalog/a"))
            self.assertEqual(row["status"], "live")
            self.assertEqual(store.run_state()["page_budget_left"], crawler.config.page_budget - 1)
            self.assertEqual(store.run_state()["retry_budget_left"], crawler.config.retry_budget - 1)
            sent = origin.requests_for("/catalog/a")
            self.assertGreaterEqual(len(sent), 2)
            self.assertGreaterEqual(sent[1]["now"] - sent[0]["now"], 30.0)
            self.assertNotIn("If-Modified-Since", sent[-1]["headers"])
            store.close()

    def test_robots_failure_modes_and_cache(self) -> None:
        page = Page("/catalog/a", _html("alpha"), headers={"Content-Type": "text/html; charset=utf-8"})
        blocked, origin, store, _clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
        origin.push_fault("/robots.txt", Fault(status=503))
        blocked.run()
        self.assertEqual(origin.requests_for("/catalog/a"), [])
        self.assertEqual(store.get(origin.absolute("/catalog/a"))["status"], "disallowed")
        # The fail-closed policy lasts one run; the next run asks for robots.txt again.
        blocked.run()
        self.assertEqual(len(origin.requests_for("/robots.txt")), 2)
        store.close()

        timed, origin, store, _clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
        origin.push_fault("/robots.txt", Fault(timeout=True))
        timed.run()
        self.assertEqual(origin.requests_for("/catalog/a"), [])
        store.close()

        opened, origin, store, clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
        origin.push_fault("/robots.txt", Fault(status=404))
        opened.run()
        self.assertEqual(len(origin.requests_for("/catalog/a")), 1)
        before = len(origin.requests_for("/robots.txt"))
        opened.run()
        self.assertEqual(len(origin.requests_for("/robots.txt")), before)
        clock.sleep(86400.0)
        opened.run()
        self.assertEqual(len(origin.requests_for("/robots.txt")), before + 1)
        store.close()

    def test_robots_redirect_limit(self) -> None:
        page = Page("/catalog/a", _html("alpha"), headers={"Content-Type": "text/html; charset=utf-8"})
        crawler, origin, store, _clock = helpers.make_lab(
            [page], ALLOW, Config(max_robots_redirects=5), seed_path="/catalog/a"
        )
        origin.push_fault(
            "/robots.txt",
            Fault(status=302, headers={"Location": "https://rules.example/0"}),
        )
        for hop in range(6):
            nxt = f"https://rules.example/{hop + 1}"
            origin.extra[f"https://rules.example/{hop}"] = Response(
                302, {"Location": nxt}, b"", f"https://rules.example/{hop}"
            )
        crawler.run()
        self.assertEqual(origin.requests_for("/catalog/a"), [])
        self.assertEqual(store.host(crawler.config.origin)["policy"], "disallow_all")
        store.close()

        crawler, origin, store, _clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
        origin.push_fault(
            "/robots.txt",
            Fault(status=302, headers={"Location": "https://rules.example/robots.txt"}),
        )
        origin.extra["https://rules.example/robots.txt"] = Response(
            200,
            {"Content-Type": "text/plain", "Cache-Control": "max-age=3600"},
            b"User-agent: FrontierBot\nAllow: /\n",
            "https://rules.example/robots.txt",
        )
        crawler.run()
        self.assertEqual(len(origin.requests_for("/catalog/a")), 1)
        self.assertEqual(store.host(crawler.config.origin)["policy"], "rules")
        store.close()

    def test_conditional_get_keeps_the_strong_checksum(self) -> None:
        body = _html("alpha")
        page = Page(
            "/catalog/a",
            body,
            headers={"Content-Type": "text/html; charset=utf-8"},
            etag='"a-1"',
            last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        )
        crawler, origin, store, clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
        crawler.run()
        url = origin.absolute("/catalog/a")
        first = store.get(url)
        self.assertEqual(first["sha256"], sha256_hex(body))
        clock.sleep(float(crawler.config.horizon_seconds))
        crawler.run()
        sent = origin.requests_for("/catalog/a")[-1]["headers"]
        self.assertIn("If-None-Match", sent)
        self.assertNotIn("If-Modified-Since", sent)
        second = store.get(url)
        self.assertEqual(second["sha256"], first["sha256"])
        self.assertEqual(second["byte_change_count"], 0)
        self.assertTrue(second["byte_identity_known"])
        store.close()

    def test_weak_304_does_not_move_the_material_count(self) -> None:
        body = BASE.encode("utf-8")
        page = Page(
            "/catalog/a",
            body,
            headers={"Content-Type": "text/html; charset=utf-8"},
            etag='"v1"',
            weak=True,
        )
        config = Config(simhash_k=23, material_detection=True)
        crawler, origin, store, clock = helpers.make_lab(
            [page], ALLOW, config, seed_path="/catalog/a"
        )
        crawler.run()
        url = origin.absolute("/catalog/a")
        self.assertFalse(store.get(url)["byte_identity_known"])
        clock.sleep(float(config.horizon_seconds))
        crawler.run()
        row = store.get(url)
        self.assertEqual(row["material_change_count"], 0)
        self.assertEqual(row["sha256"], sha256_hex(body))
        self.assertFalse(row["byte_identity_known"])
        kinds = [event["kind"] for event in store.events() if event["url"] == url]
        self.assertIn("validator_not_modified", kinds)
        store.close()

    def test_content_encoding_and_charsets(self) -> None:
        raw = BASE.encode("utf-8")
        for encoding in ("gzip", "deflate", "compress"):
            page = Page(
                "/catalog/a",
                raw,
                headers={"Content-Type": "text/html; charset=utf-8"},
                encoding=encoding,
                etag='"encoded"',
            )
            crawler, origin, store, _clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
            crawler.run()
            self.assertEqual(store.get(origin.absolute("/catalog/a"))["sha256"], sha256_hex(raw))
            store.close()

        missing = Page(
            "/catalog/a",
            b"<html><head><meta charset=\"utf-8\"></head><body><p>Cafe</p></body></html>",
            headers={"Content-Type": "text/html"},
            etag='"plain"',
        )
        crawler, origin, store, _clock = helpers.make_lab([missing], ALLOW, seed_path="/catalog/a")
        crawler.run()
        row = store.get(origin.absolute("/catalog/a"))
        self.assertIsNone(row["simhash"])
        self.assertEqual(row["sha256"], sha256_hex(missing.body))
        self.assertEqual(row["charset_unknown"], 1)
        store.close()

        linen = Page(
            "/catalog/a",
            LINEN.encode("iso-8859-1"),
            headers={"Content-Type": "text/html; charset=iso-8859-1"},
            etag='"linen"',
        )
        crawler, origin, store, _clock = helpers.make_lab([linen], ALLOW, seed_path="/catalog/a")
        crawler.run()
        row = store.get(origin.absolute("/catalog/a"))
        self.assertEqual(row["simhash"], format_simhash(simhash_text(LINEN)))
        self.assertEqual(row["charset"], "iso-8859-1")
        store.close()

    def test_capacity_evicts_the_unprotected_leaf_and_410_frees_a_slot(self) -> None:
        root_body = b"<html><body><a href=\"/catalog/mid\">mid</a></body></html>"
        config = Config(page_budget=3, collection_capacity=2)
        pages = [
            Page("/catalog/root", root_body, headers={"Content-Type": "text/html; charset=utf-8"}, etag='"root"'),
            Page("/catalog/leaf", _html("leaf"), headers={"Content-Type": "text/html; charset=utf-8"}, etag='"leaf"'),
            Page("/catalog/mid", _html("mid"), headers={"Content-Type": "text/html; charset=utf-8"}, etag='"mid"'),
        ]
        crawler, origin, store, clock = helpers.make_lab(pages, ALLOW, config, seed_path="/catalog/unused")
        now = clock.now
        store.begin_horizon(now, config)
        store.seed_live(
            origin.absolute("/catalog/root"),
            origin=config.origin,
            depth=0,
            status="live",
            observation_count=1,
            synced_at=now - 50,
            next_due_at=now - 1,
            body=root_body,
            charset="utf-8",
            etag='"root"',
        )
        store.seed_live(
            origin.absolute("/catalog/leaf"),
            origin=config.origin,
            depth=2,
            status="live",
            observation_count=1,
            synced_at=now - 10,
            next_due_at=now - 1,
            body=_html("leaf"),
            charset="utf-8",
            etag='"leaf"',
        )
        crawler.run()
        self.assertEqual(store.get(origin.absolute("/catalog/leaf"))["status"], "evicted")
        self.assertEqual(store.get(origin.absolute("/catalog/mid"))["status"], "live")
        self.assertEqual(store.get(origin.absolute("/catalog/root"))["status"], "live")
        store.close()

        gone = Page("/catalog/a", b"missing", status=410, headers={"Content-Type": "text/plain"})
        crawler, origin, store, _clock = helpers.make_lab(
            [gone], ALLOW, Config(collection_capacity=1, page_budget=2), seed_path="/catalog/a"
        )
        crawler.run()
        self.assertEqual(store.get(origin.absolute("/catalog/a"))["status"], "gone")
        self.assertEqual(store.occupants(), 0)
        admitted = store.admit(
            origin.absolute("/catalog/b"),
            origin=crawler.config.origin,
            depth=0,
            now=_clock.now,
            capacity=1,
            protected=set(),
        )
        self.assertEqual(admitted, "admitted")
        store.close()

    def test_variant_change_and_undecodable_body(self) -> None:
        page = Page(
            "/catalog/a",
            _html("alpha"),
            headers={"Content-Type": "text/html; charset=utf-8"},
            etag='"a"',
        )
        crawler, origin, store, clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
        crawler.run()
        crawler.config.accept = "text/plain"
        clock.sleep(float(crawler.config.horizon_seconds))
        crawler.run()
        sent = origin.requests_for("/catalog/a")[-1]["headers"]
        self.assertNotIn("If-None-Match", sent)
        self.assertEqual(sent["Accept"], "text/plain")
        store.close()

        crawler, origin, store, _clock = helpers.make_lab([page], ALLOW, seed_path="/catalog/a")
        origin.push_fault(
            "/catalog/a",
            Fault(
                status=200,
                headers={"Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"},
                body=b"not-gzip",
            ),
        )
        crawler.run()
        self.assertEqual(store.run_state()["page_budget_left"], crawler.config.page_budget)
        self.assertTrue(any(event["kind"] == "decode_error" for event in store.events()))
        self.assertNotEqual(store.get(origin.absolute("/catalog/a"))["status"], "live")
        store.close()

    def test_two_edits_inside_one_sample_count_once_and_censor(self) -> None:
        page = Page(
            "/catalog/a",
            BASE.encode("utf-8"),
            headers={"Content-Type": "text/html; charset=utf-8"},
            etag='"v1"',
        )
        config = Config(simhash_k=23, material_detection=True)
        crawler, origin, store, clock = helpers.make_lab([page], ALLOW, config, seed_path="/catalog/a")
        url = origin.absolute("/catalog/a")
        crawler.run()
        clock.sleep(float(config.horizon_seconds))
        origin.replace_body("/catalog/a", AD_EDIT.encode("utf-8"), clock.now - 3600.0, etag='"v2"')
        origin.replace_body("/catalog/a", PARAGRAPH_EDIT.encode("utf-8"), clock.now - 60.0, etag='"v3"')
        crawler.run()
        self.assertEqual(len(origin.changes), 2)
        row = store.get(url)
        self.assertEqual(row["material_change_count"], 1)
        self.assertEqual(row["byte_change_count"], 1)
        self.assertEqual(row["rate_censored"], 1)
        span = row["last_observed_at"] - row["first_observed_at"]
        self.assertAlmostEqual(row["lambda_hat"], max(1.0 / span, 1.0 / config.horizon_seconds))

        clock.sleep(float(config.horizon_seconds))
        crawler.run()
        row = store.get(url)
        self.assertEqual(row["material_change_count"], 1)
        self.assertEqual(row["observation_count"], 3)
        self.assertEqual(row["rate_censored"], 0)
        self.assertAlmostEqual(row["lambda_hat"], 1.0 / (row["last_observed_at"] - row["first_observed_at"]))
        store.close()

    def test_audit_floor_reaches_a_zero_rate_page_when_the_budget_binds(self) -> None:
        config = Config(page_budget=10)
        paths = [f"/catalog/hot-{index:02d}" for index in range(11)] + ["/catalog/quiet"]
        pages = [
            Page(path, _html(path), headers={"Content-Type": "text/html; charset=utf-8"}, etag=f'"{path}"')
            for path in paths
        ]
        crawler, origin, store, clock = helpers.make_lab(pages, ALLOW, config, seed_path="/catalog/quiet")
        now = clock.now
        for index, path in enumerate(paths):
            quiet = path == "/catalog/quiet"
            store.seed_live(
                origin.absolute(path),
                origin=config.origin,
                depth=1,
                observation_count=1,
                synced_at=now - (7200.0 if quiet else 3600.0 - index),
                next_due_at=now - 1,
                lambda_hat=0.0 if quiet else 1e-5,
                etag=f'"{path}"',
            )
        crawler.run()
        fetched = {path for path in paths if origin.requests_for(path)}
        self.assertIn("/catalog/quiet", fetched)
        self.assertEqual(len(fetched), config.page_budget)
        self.assertEqual(store.run_state()["page_budget_left"], 0)
        # The budget was binding: two positive-rate pages waited, the zero-rate page did not.
        self.assertEqual(len([path for path in paths if path not in fetched]), 2)
        store.close()

    def test_malformed_server_input_does_not_stop_the_run(self) -> None:
        body = (
            b'<html><body><a href="http://catalog.example.invalid:abc/x">bad port</a>'
            b'<a href="/catalog/b">b</a><p>caf\xe9</p></body></html>'
        )
        pages = [
            Page("/catalog/a", body, headers={"Content-Type": "text/html; charset=utf-8"}, etag='"a"'),
            Page("/catalog/b", _html("beta"), headers={"Content-Type": "text/html; charset=utf-8"}, etag='"b"'),
        ]
        crawler, origin, store, _clock = helpers.make_lab(pages, ALLOW, seed_path="/catalog/a")
        origin.robots_headers = {"Content-Type": "text/plain", "Expires": "0"}
        origin.push_fault("/catalog/a", Fault(status=503, headers={"Retry-After": "soon"}))
        crawler.run()
        retries = [json.loads(event["detail"]) for event in store.events() if event["kind"] == "retry"]
        self.assertEqual([item["reason"] for item in retries], ["status_503"])
        row = store.get(origin.absolute("/catalog/a"))
        self.assertEqual(row["status"], "live")
        self.assertEqual(row["sha256"], sha256_hex(body))
        self.assertIsNone(row["simhash"])
        self.assertEqual(row["charset_unknown"], 1)
        self.assertEqual(store.get(origin.absolute("/catalog/b"))["status"], "live")
        before = len(origin.requests_for("/robots.txt"))
        crawler.run()
        self.assertEqual(len(origin.requests_for("/robots.txt")), before + 1)
        store.close()

    def test_catalog_redirects_stay_on_the_allowlist(self) -> None:
        pages = [
            Page("/catalog/", b'<a href="/catalog/old">old</a><a href="/catalog/away">away</a>',
                 headers={"Content-Type": "text/html; charset=utf-8"}, etag='"index"'),
            Page("/catalog/old", b"", status=301, headers={"Location": "/catalog/new"}, etag='"old"'),
            Page("/catalog/away", b"", status=302, headers={"Location": "https://evil.example/x"}),
            Page("/catalog/new", _html("new"), headers={"Content-Type": "text/html; charset=utf-8"}, etag='"new"'),
        ]
        crawler, origin, store, _clock = helpers.make_lab(pages, ALLOW)
        crawler.run()
        new = store.get(origin.absolute("/catalog/new"))
        self.assertEqual(new["status"], "live")
        self.assertEqual(new["etag"], '"new"')
        self.assertNotIn("If-None-Match", origin.requests_for("/catalog/new")[0]["headers"])
        self.assertNotEqual(store.get(origin.absolute("/catalog/old"))["status"], "live")
        self.assertFalse(any("evil.example" in item["url"] for item in origin.request_log))
        self.assertIsNone(store.get("https://evil.example/x"))
        off = [json.loads(event["detail"])["href"] for event in store.events() if event["kind"] == "off_origin"]
        self.assertIn("https://evil.example/x", off)
        store.close()

    def test_demo_horizon_matches_the_fixture_story(self) -> None:
        config = load_config(helpers.EXAMPLES / "config.json")
        origin = load_site(helpers.EXAMPLES / "site.json")
        from incremental_crawl_lab.clock import ManualClock
        from incremental_crawl_lab.crawl import Crawler
        from incremental_crawl_lab.store import Store

        store = Store.open(None, durable=False)
        clock = ManualClock()
        crawler = Crawler(config, store, origin, clock)
        start = clock.now
        for week in range(2):
            if week:
                clock.sleep(start + week * config.horizon_seconds - clock.now)
                origin.apply_horizon(week, clock.now)
            crawler.run()
        report = build_report(
            store,
            origin,
            config,
            dry_run=True,
            state_dir=None,
            requests=crawler.transmissions,
            window=(crawler.first_horizon_start, crawler.last_horizon_end),
        )
        self.assertEqual(report["requests"], 10)
        self.assertEqual(report["not_modified_304"], 2)
        self.assertEqual(report["material_change_count"], 1)
        self.assertEqual(report["cosmetic_change_count"], 1)
        self.assertEqual(report["byte_change_count"], 2)
        self.assertEqual(report["robots_blocks"], 2)
        self.assertEqual(report["retries"], 0)
        self.assertEqual(report["page_budget_left"], 4)
        self.assertEqual(report["rate_censored_urls"], 1)
        self.assertEqual(report["simhash_k"], 23)
        self.assertEqual(origin.requests_for("/private/cost"), [])
        self.assertEqual(origin.requests_for("/secret"), [])
        self.assertFalse(any("evil.example" in item["url"] for item in origin.request_log))
        wool = store.get(origin.absolute("/catalog/wool-coat"))
        notes = store.get(origin.absolute("/catalog/field-notes"))
        linen = store.get(origin.absolute("/catalog/linen-shirt"))
        self.assertEqual(wool["cosmetic_change_count"], 1)
        self.assertEqual(wool["material_change_count"], 0)
        self.assertEqual(notes["material_change_count"], 1)
        self.assertEqual(notes["rate_censored"], 1)
        self.assertEqual(linen["simhash"], format_simhash(simhash_text(LINEN)))
        self.assertEqual(linen["charset"], "iso-8859-1")
        store.close()


if __name__ == "__main__":
    unittest.main()
