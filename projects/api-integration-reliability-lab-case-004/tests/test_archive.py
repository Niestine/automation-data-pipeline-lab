"""Sealed archive pages, cursor checkpoints, and crash rollback."""

from __future__ import annotations

import json

from helpers import LabCase
from lotcycle.archive import build_collection, merge_entry
from lotcycle.errors import CrashBeforeCommit
from lotcycle.httputil import Request, parse_link
from lotcycle.world import make_entries


def _entries(count: int, prefix: str = "ex", lot: str = "L") -> list[dict]:
    return make_entries(
        {"count": count, "id_prefix": prefix, "lot_prefix": lot}
    )


class ArchiveTest(LabCase):
    def _bearer(self, client_id: str = "handheld-north") -> dict[str, str]:
        token = self.lab.clients[client_id].access_token
        return {"authorization": "Bearer " + token}

    def test_links_and_sync_cover_every_entry(self) -> None:
        head = self.lab.resources["lot-ledger"].handle(
            Request("GET", "/lot-ledger/entries", self._bearer())
        )
        self.assertEqual(head.status, 200)
        links = parse_link(head.headers["link"])
        self.assertEqual(links["self"], "/lot-ledger/entries")
        self.assertEqual(links["current"], "/lot-ledger/entries")
        self.assertEqual(links["prev-archive"], "/lot-ledger/archives/ex-100")
        self.assertNotIn("next-archive", links)
        self.assertEqual(len(head.json()["entries"]), 20)

        middle = self.lab.resources["lot-ledger"].handle(
            Request("GET", "/lot-ledger/archives/ex-100", self._bearer())
        )
        middle_links = parse_link(middle.headers["link"])
        self.assertEqual(middle_links["prev-archive"], "/lot-ledger/archives/ex-050")
        self.assertNotIn("next-archive", middle_links)
        self.assertEqual(len(middle.json()["entries"]), 50)

        oldest = self.lab.resources["lot-ledger"].handle(
            Request("GET", "/lot-ledger/archives/ex-050", self._bearer())
        )
        oldest_links = parse_link(oldest.headers["link"])
        self.assertNotIn("prev-archive", oldest_links)
        self.assertEqual(oldest_links["next-archive"], "/lot-ledger/archives/ex-100")
        self.assertEqual(oldest.json()["entries"][0]["id"], "ex-001")
        self.assertEqual(oldest.json()["entries"][0]["celsius"], -19)
        self.assertEqual(len(oldest.json()["entries"]), 50)

        client = self.north()
        found = client.sync()
        server = self.lab.resources["lot-ledger"].entry_ids("handheld-north")
        self.assertEqual(found, server)
        self.assertEqual(len(found), 120)
        self.assertTrue(client.sync_complete)
        self.assertFalse(client.reconstruction_incomplete)
        self.assertEqual(self.lab.store.checkpoint_count("handheld-north", "lot-ledger"), 2)
        self.assertTrue(self.lab.store.checkpointed("handheld-north", "lot-ledger", "ex-100"))
        self.assertTrue(self.lab.store.checkpointed("handheld-north", "lot-ledger", "ex-050"))
        self.assertEqual(client.sync(), found)

    def test_crash_before_archive_checkpoint_rolls_back(self) -> None:
        client = self.north()
        with self.assertRaises(CrashBeforeCommit):
            client.sync(crash_on_archive=True)
        head_ids = {f"ex-{index:03d}" for index in range(101, 121)}
        self.assertEqual(self.lab.store.local_ids("handheld-north", "lot-ledger"), head_ids)
        self.assertFalse(self.lab.store.checkpointed("handheld-north", "lot-ledger", "ex-100"))
        self.assertFalse(self.lab.store.checkpointed("handheld-north", "lot-ledger", "ex-050"))
        restored = client.sync()
        self.assertEqual(restored, self.lab.resources["lot-ledger"].entry_ids("handheld-north"))
        self.assertEqual(len(restored), 120)
        count = self.lab.store.con.execute(
            "SELECT COUNT(*) AS n FROM local_entries WHERE client_id = ?",
            ("handheld-north",),
        ).fetchone()["n"]
        self.assertEqual(count, 120)
        self.assertTrue(client.sync_complete)

    def test_sealed_write_and_later_insert_stay_on_the_head(self) -> None:
        ledger = self.lab.resources["lot-ledger"]
        sealed = ledger.page_entry_ids("handheld-north", "ex-050")
        self.assertEqual(len(sealed), 50)
        denied = ledger.handle(
            Request(
                "POST",
                "/lot-ledger/archives/ex-050/entries",
                self._bearer(),
                b"{}",
            )
        )
        self.assertEqual(denied.status, 409)
        self.assertEqual(ledger.page_entry_ids("handheld-north", "ex-050"), sealed)
        ledger.add_entry(
            "handheld-north",
            {"celsius": -18, "id": "ex-121", "lot": "L-121", "updated": 121},
        )
        head = ledger.page_entry_ids("handheld-north", None)
        self.assertIn("ex-121", head)
        self.assertNotIn("ex-121", ledger.page_entry_ids("handheld-north", "ex-050"))
        self.assertEqual(ledger.page_entry_ids("handheld-north", "ex-050"), sealed)
        unknown = ledger.handle(
            Request(
                "POST",
                "/lot-ledger/archives/ex-999/entries",
                self._bearer(),
                b"{}",
            )
        )
        self.assertEqual(unknown.status, 404)

    def test_hidden_archive_stops_reconstruction(self) -> None:
        for how, status in (("gone", 410), ("forbidden", 403), ("missing", 404)):
            with self.subTest(how=how):
                from helpers import build

                lab = build()
                try:
                    before = lab.resources["lot-ledger"].entry_ids("handheld-north")
                    self.assertEqual(len(before), 120)
                    lab.resources["lot-ledger"].hide_archive("handheld-north", "ex-100", how)
                    probe = lab.resources["lot-ledger"].handle(
                        Request(
                            "GET",
                            "/lot-ledger/archives/ex-100",
                            {"authorization": "Bearer " + lab.clients["handheld-north"].access_token},
                        )
                    )
                    self.assertEqual(probe.status, status)
                    client = lab.clients["handheld-north"]
                    client.sync()
                    self.assertTrue(client.reconstruction_incomplete)
                    self.assertFalse(client.sync_complete)
                    self.assertFalse(lab.store.checkpointed("handheld-north", "lot-ledger", "ex-100"))
                    self.assertFalse(lab.store.checkpointed("handheld-north", "lot-ledger", "ex-050"))
                    local = lab.store.local_ids("handheld-north", "lot-ledger")
                    self.assertIn("ex-120", local)
                    self.assertNotIn("ex-001", local)
                    self.assertTrue(local.issubset(before))
                finally:
                    lab.close()

    def test_merge_keeps_the_newer_entry_and_the_newer_document(self) -> None:
        older = {"celsius": -19, "doc_updated": 5, "id": "ex-001", "lot": "L-001", "updated": 1}
        newer = {"celsius": -18, "doc_updated": 1, "id": "ex-001", "lot": "L-002", "updated": 2}
        self.assertIs(merge_entry(older, newer), newer)
        self.assertIs(merge_entry(newer, older), newer)
        low = {"celsius": -18, "doc_updated": 3, "id": "ex-001", "lot": "L-003", "updated": 2}
        high = {"celsius": -17, "doc_updated": 4, "id": "ex-001", "lot": "L-004", "updated": 2}
        self.assertIs(merge_entry(low, high), high)
        self.assertIs(merge_entry(high, low), high)

        client = self.north()
        client.sync()
        client._upsert(
            {"celsius": -10, "doc_updated": 999, "id": "ex-001", "lot": "L-009", "updated": 50}
        )
        stored = self.lab.store.local_entry("handheld-north", "lot-ledger", "ex-001")
        self.assertEqual(stored["updated"], 50)
        client._upsert(
            {"celsius": -10, "doc_updated": 1, "id": "ex-001", "lot": "L-008", "updated": 50}
        )
        payload = json.loads(
            self.lab.store.local_entry("handheld-north", "lot-ledger", "ex-001")["payload"]
        )
        self.assertEqual(payload["lot"], "L-009")

    def test_page_edges_for_a_short_collection(self) -> None:
        partial = build_collection(_entries(5), 2)
        self.assertEqual(
            [entry["id"] for entry in partial.archives["ex-002"].entries],
            ["ex-001", "ex-002"],
        )
        self.assertIsNone(partial.archives["ex-002"].prev_cursor)
        self.assertEqual(partial.archives["ex-002"].next_cursor, "ex-004")
        self.assertEqual(partial.archives["ex-004"].prev_cursor, "ex-002")
        self.assertIsNone(partial.archives["ex-004"].next_cursor)
        self.assertEqual([entry["id"] for entry in partial.head.entries], ["ex-005"])
        self.assertEqual(partial.head.prev_cursor, "ex-004")
        self.assertFalse(partial.head.sealed)
        self.assertIsInstance(partial.forbidden, set)

        exact = build_collection(_entries(4), 2)
        self.assertEqual(list(exact.archives), ["ex-002"])
        self.assertEqual([entry["id"] for entry in exact.head.entries], ["ex-003", "ex-004"])
        self.assertEqual(exact.head.prev_cursor, "ex-002")
        self.assertIsNone(exact.archives["ex-002"].next_cursor)

    def test_dry_sync_does_not_checkpoint(self) -> None:
        client = self.north()
        found = client.sync(commit=False)
        self.assertEqual(len(found), 120)
        self.assertEqual(self.lab.store.checkpoint_count("handheld-north", "lot-ledger"), 0)
        self.assertEqual(len(self.lab.store.local_ids("handheld-north", "lot-ledger")), 0)
        self.assertFalse(client.sync_complete)
