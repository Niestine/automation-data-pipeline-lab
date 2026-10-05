"""Cursor contract: empty token ends the walk; the snapshot does not."""

from __future__ import annotations

import base64
import json
import unittest

import helpers
from plot_allotment.schema import MAX_PAGE_SIZE, TOKEN_TTL_SECONDS
from plot_allotment.walks import keyset_slice, offset_slice

PARENT = helpers.PARENT


class PaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lab = helpers.Lab(seed=1)
        self.addCleanup(self.lab.close)

    def test_short_page_is_not_the_end_when_the_server_withholds_it(self) -> None:
        self.lab.add(helpers.lot("lot-01", 1), helpers.lot("lot-02", 2), helpers.lot("lot-03", 3))
        self.lab.service.withhold_terminal = 1
        rows = self.lab.client.walk(PARENT, page_size=10)
        self.assertEqual([row["resource_id"] for row in rows], ["lot-01", "lot-02", "lot-03"])
        self.assertEqual(self.lab.client.calls, 2)
        self.assertEqual(
            [(item["count"], item["end"]) for item in self.lab.service.page_trace],
            [(3, False), (0, True)],
        )

    def test_empty_page_with_a_token_is_followed(self) -> None:
        self.lab.add(helpers.lot("lot-01", 1), helpers.lot("lot-02", 2), helpers.lot("lot-03", 3))
        self.lab.service.empty_page_once = True
        rows = self.lab.client.walk(PARENT, page_size=10)
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.lab.client.calls, 2)
        self.assertFalse(self.lab.service.page_trace[0]["end"])
        self.assertEqual(self.lab.service.page_trace[0]["count"], 0)
        self.assertTrue(self.lab.service.page_trace[1]["end"])

    def test_offset_misses_the_shifted_row_and_the_snapshot_keeps_it(self) -> None:
        for name, sort_key in (("lot-a", 1), ("lot-b", 2), ("lot-c", 3), ("lot-d", 4)):
            self.lab.add(helpers.lot(name, sort_key))
        first = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "2"},
                helpers.bearer(),
            )
        )
        self.assertEqual(first.status, 200)
        payload = json.loads(first.body)
        self.lab.store.delete_source(PARENT, "lot-a")
        offset_ids = [row["resource_id"] for row in self.lab.store.offset_page(PARENT, 2, 2)]
        self.assertNotIn("lot-c", offset_ids)
        second = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "2", "page_token": payload["next_page_token"]},
                helpers.bearer(),
            )
        )
        followed = json.loads(second.body)["resources"]
        self.assertEqual([row["resource_id"] for row in followed], ["lot-c", "lot-d"])

    def test_snapshot_ignores_a_mid_walk_insert_and_a_bumped_key(self) -> None:
        for name, sort_key in (("lot-a", 1), ("lot-b", 2), ("lot-c", 3), ("lot-d", 4)):
            self.lab.add(helpers.lot(name, sort_key))
        first = json.loads(
            self.lab.service.handle(
                helpers.Request(
                    "GET",
                    f"/v1/{PARENT}/resources",
                    {"page_size": "2"},
                    helpers.bearer(),
                )
            ).body
        )
        self.lab.add(helpers.lot("lot-m", 3))
        self.lab.store.update_source_sort(PARENT, "lot-a", 9)
        second = json.loads(
            self.lab.service.handle(
                helpers.Request(
                    "GET",
                    f"/v1/{PARENT}/resources",
                    {"page_size": "2", "page_token": first["next_page_token"]},
                    helpers.bearer(),
                )
            ).body
        )
        seen = [row["resource_id"] for row in first["resources"] + second["resources"]]
        self.assertEqual(seen, ["lot-a", "lot-b", "lot-c", "lot-d"])
        self.assertNotIn("lot-m", seen)
        self.assertEqual(seen.count("lot-a"), 1)
        self.assertNotIn(first["snapshot_id"], first["next_page_token"])

    def test_live_keyset_misses_a_late_earlier_key(self) -> None:
        for name, sort_key in (("lot-a", 1), ("lot-b", 2), ("lot-c", 3), ("lot-d", 4)):
            self.lab.add(helpers.lot(name, sort_key))
        page = self.lab.store.live_keyset_page(PARENT, None, 2)
        cursor = (page[-1]["sort_key"], page[-1]["resource_id"])
        self.lab.add(helpers.lot("lot-m", 1))
        self.lab.store.update_source_sort(PARENT, "lot-a", 11)
        rest = self.lab.store.live_keyset_page(PARENT, cursor, 10)
        exported = [row["resource_id"] for row in page + rest]
        self.assertNotIn("lot-m", exported)
        self.assertEqual(exported.count("lot-a"), 2)
        final = set(self.lab.store.live_ids(PARENT))
        with self.assertRaises(AssertionError):
            self.assertEqual(set(exported), final)

    def test_page_size_rules_token_binding_and_caller(self) -> None:
        self.lab.add(helpers.lot("lot-01", 1), helpers.lot("lot-02", 2), helpers.lot("lot-03", 3))
        negative = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "-1"},
                helpers.bearer(),
            )
        )
        self.assertEqual(negative.status, 400)
        omitted = self.lab.service.handle(
            helpers.Request("GET", f"/v1/{PARENT}/resources", {}, helpers.bearer())
        )
        self.assertEqual(self.lab.service.last_limit, 50)
        self.assertEqual(omitted.status, 200)
        zero = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "0"},
                helpers.bearer(),
            )
        )
        self.assertEqual(zero.status, 200)
        self.assertEqual(self.lab.service.last_limit, 50)

        for index in range(1001):
            self.lab.add(helpers.lot(f"row-{index:04d}", index + 10))
        coerced = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "100000", "filter": ""},
                helpers.bearer(),
            )
        )
        # 3 + 1001 rows share this parent, so the coerced page is full and
        # still carries a next token.
        self.assertEqual(coerced.status, 200)
        body = json.loads(coerced.body)
        self.assertEqual(len(body["resources"]), MAX_PAGE_SIZE)
        self.assertTrue(body["next_page_token"])
        self.assertEqual(self.lab.service.last_limit, MAX_PAGE_SIZE)
        token = body["next_page_token"]
        snapshot_id = body["snapshot_id"]
        self.assertNotIn(snapshot_id, token)
        encoded = base64.urlsafe_b64encode(b"1000:row-0990").decode("ascii")
        self.assertNotEqual(token, encoded)

        changed = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "1", "page_token": token},
                helpers.bearer(),
            )
        )
        self.assertEqual(changed.status, 200)
        self.assertEqual(len(json.loads(changed.body)["resources"]), 1)

        rejected = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "1", "page_token": token, "filter": "note=closed"},
                helpers.bearer(),
            )
        )
        self.assertEqual(rejected.status, 400)

        self.lab.clock.advance(TOKEN_TTL_SECONDS)
        expired = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "1", "page_token": token},
                helpers.bearer(),
            )
        )
        self.assertEqual(expired.status, 400)
        self.assertIn(b"expired", expired.body)

    def test_other_caller_cannot_use_the_token_and_a_fresh_token_still_works(self) -> None:
        self.lab.add(helpers.lot("lot-01", 1), helpers.lot("lot-02", 2))
        first = json.loads(
            self.lab.service.handle(
                helpers.Request(
                    "GET",
                    f"/v1/{PARENT}/resources",
                    {"page_size": "1"},
                    helpers.bearer(),
                )
            ).body
        )
        denied = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "1", "page_token": first["next_page_token"]},
                helpers.bearer("desk-b"),
            )
        )
        self.assertEqual(denied.status, 403)
        self.assertNotIn(b"lot-02", denied.body)
        allowed = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"page_size": "1", "page_token": first["next_page_token"]},
                helpers.bearer(),
            )
        )
        self.assertEqual(allowed.status, 200)
        self.assertEqual(json.loads(allowed.body)["resources"][0]["resource_id"], "lot-02")

    def test_keyset_seek_does_not_walk_the_offset_prefix(self) -> None:
        keys = list(range(5000))
        _offset_page, offset_cost = offset_slice(keys, 1000, 20)
        cursor = keys[999]
        keyset_page, keyset_cost = keyset_slice(keys, cursor, 20)
        self.assertEqual(offset_cost, 1020)
        self.assertEqual(keyset_cost, 20)
        self.assertEqual(keyset_page[0], 1000)
        self.assertLess(keyset_cost, offset_cost)

    def test_offset_query_is_rejected(self) -> None:
        response = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"offset": "10"},
                helpers.bearer(),
            )
        )
        self.assertEqual(response.status, 400)

    def test_skip_query_is_rejected(self) -> None:
        response = self.lab.service.handle(
            helpers.Request(
                "GET",
                f"/v1/{PARENT}/resources",
                {"skip": "10", "page_size": "2"},
                helpers.bearer(),
            )
        )
        self.assertEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main()
