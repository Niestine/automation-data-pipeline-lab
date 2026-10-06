import unittest

import helpers  # noqa: F401

from edition_gate.fingerprint import parse_fingerprint, resolution_fingerprint
from edition_gate.model import column, schema
from edition_gate.pipeline import ingest_bytes
from edition_gate.registry import (
    Registry,
    assess_operators,
    explain_resolution,
    full_pair,
    full_transitive,
    rewrite_names,
)


def _part(number, extra=()):
    columns = [column("part_id", required=True, null_tokens=())]
    columns.extend(extra)
    return schema(number, columns)


def _note(number, publication, **kwargs):
    extra = []
    if publication != "absent":
        extra.append(
            column(
                "note",
                required=True,
                publication=publication,
                null_tokens=(),
                **kwargs,
            )
        )
    return _part(number, extra)


class RegistryTests(unittest.TestCase):
    def test_parse_fingerprint_ignores_defaults_and_aliases(self):
        plain = schema(1, [column("email", required=True, null_tokens=())])
        filled = schema(
            1,
            [column("email", required=True, has_default=True, default="a@example.test", aliases=("mail",), null_tokens=())],
        )
        self.assertEqual(parse_fingerprint(plain), parse_fingerprint(filled))
        self.assertNotEqual(resolution_fingerprint(plain), resolution_fingerprint(filled))

    def test_email_drop_is_adjacent_full_and_transitive_rejected(self):
        v1 = _part(1, [column("email", required=True, null_tokens=())])
        v2 = _part(
            2,
            [column("email", required=True, has_default=True, default="none@example.test", null_tokens=())],
        )
        v3 = _part(3)
        ops12 = [{"op": "set_default", "name": "email", "version": 2}]
        ops13 = ops12 + [{"op": "drop_column", "name": "email", "version": 3}]
        self.assertTrue(full_pair(v1, v2, ops12))
        self.assertTrue(full_pair(v2, v3, ops13))
        self.assertFalse(full_pair(v1, v3, ops13))
        self.assertFalse(full_transitive(v3, [v1, v2], ops13))
        registry = Registry()
        self.assertTrue(registry.register(v1, [])["ok"])
        self.assertTrue(registry.register(v2, [{"op": "set_default", "name": "email"}])["ok"])
        self.assertEqual(parse_fingerprint(registry.get(1)), parse_fingerprint(registry.get(2)))
        self.assertNotEqual(resolution_fingerprint(registry.get(1)), resolution_fingerprint(registry.get(2)))
        denied = registry.register(v3, [{"op": "drop_column", "name": "email"}])
        self.assertFalse(denied["ok"])
        self.assertIn("publication_jump", denied["reasons"])
        self.assertIn("not_information_preserving", denied["reasons"])
        self.assertIn("compatibility_rejected", denied["reasons"])
        self.assertEqual(registry.latest().number, 2)

    def test_widening_registers_and_narrowing_does_not(self):
        wide = Registry()
        self.assertTrue(wide.register(schema(1, [column("qty", datatype="integer")]), [])["ok"])
        widened = wide.register(
            schema(2, [column("qty", datatype="long")]),
            [{"op": "set_type", "name": "qty"}],
        )
        self.assertTrue(widened["ok"], widened["reasons"])
        narrow = Registry()
        self.assertTrue(narrow.register(schema(1, [column("price", datatype="decimal")]), [])["ok"])
        denied = narrow.register(
            schema(2, [column("price", datatype="integer")]),
            [{"op": "set_type", "name": "price"}],
        )
        self.assertFalse(denied["ok"])
        self.assertIn("compatibility_rejected", denied["reasons"])
        self.assertEqual(narrow.latest().number, 1)

    def test_rename_requires_an_alias_and_resolves_the_old_header(self):
        registry = Registry()
        self.assertTrue(registry.register(schema(1, [column("org_name", required=True)]), [])["ok"])
        missed = registry.register(
            schema(2, [column("organization", required=True)]),
            [{"op": "rename_column", "from": "org_name", "to": "organization"}],
        )
        self.assertIn("missed_alias", missed["reasons"])
        self.assertEqual(len(registry.versions), 1)
        accepted = registry.register(
            schema(2, [column("organization", aliases=("org_name",), required=True)]),
            [{"op": "rename_column", "from": "org_name", "to": "organization"}],
        )
        self.assertTrue(accepted["ok"], accepted["reasons"])
        loaded = ingest_bytes(registry, b"org_name\nNorthwind\n", writer_version=1, link=False)
        self.assertEqual(loaded["status"], "accepted", loaded["reason"])
        self.assertEqual(loaded["rows"][0]["cells"]["organization"]["value"], "Northwind")
        self.assertNotIn("org_name", loaded["rows"][0]["cells"])

    def test_operator_information_preservation(self):
        bare_swap = [
            {"op": "drop_column", "name": "org_name"},
            {"op": "add_column", "name": "organization"},
        ]
        self.assertIn("not_information_preserving", assess_operators(bare_swap))
        moved = [
            {"op": "drop_column", "name": "org_name"},
            {"op": "add_column", "name": "organization", "transform": {"from": "org_name"}},
        ]
        self.assertEqual(assess_operators(moved), [])
        self.assertIn(
            "not_information_preserving",
            assess_operators([{"op": "merge_columns", "target": "location", "sources": ["city", "region"]}]),
        )
        self.assertEqual(
            assess_operators(
                [
                    {
                        "op": "merge_columns",
                        "target": "location",
                        "sources": ["city", "region"],
                        "transform": {"provenance": "city"},
                    }
                ]
            ),
            [],
        )
        self.assertEqual(
            assess_operators(
                [{"op": "split_column", "source": "location", "targets": ["city", "region"], "shared_key": "part_id"}]
            ),
            [],
        )
        self.assertIn(
            "not_information_preserving",
            assess_operators([{"op": "split_column", "source": "location", "targets": ["city", "region"]}]),
        )
        self.assertIn(
            "not_information_preserving",
            assess_operators([{"op": "drop_column", "name": "note"}], [("note", "public", "absent")]),
        )
        self.assertEqual(
            assess_operators([{"op": "drop_column", "name": "note"}], [("note", "delete_only", "absent")]),
            [],
        )

    def test_rewrite_is_union_all_of_historical_names(self):
        found = rewrite_names(
            "organization",
            [
                {"op": "rename_column", "from": "org_name", "to": "organization"},
                {
                    "op": "merge_columns",
                    "target": "organization",
                    "sources": ["city", "region"],
                    "transform": {"provenance": "city"},
                },
            ],
        )
        self.assertEqual(found["form"], "UNION ALL")
        self.assertEqual(found["column"], "organization")
        self.assertEqual(found["sources"][0], "organization")
        self.assertIn("org_name", found["sources"])
        self.assertIn("city", found["sources"])
        self.assertIn("region", found["sources"])

    def test_resolution_plan_distinguishes_default_virtual_and_missing(self):
        writer = schema(1, [column("part_id"), column("bin_code")])
        reader = schema(
            2,
            [
                column("part_id"),
                column("color", has_default=True, default="green"),
                column("bin_label", virtual=True, has_default=True, default="UNBINNED"),
            ],
        )
        plan = explain_resolution(writer, reader, [])
        self.assertEqual(plan["columns"]["color"]["source"], "default")
        self.assertEqual(plan["columns"]["color"]["value"], "green")
        self.assertEqual(plan["columns"]["bin_label"]["source"], "virtual")
        self.assertIn("bin_code", plan["ignored_writer_fields"])
        missing = explain_resolution(
            schema(1, [column("part_id")]),
            schema(2, [column("part_id"), column("color", required=True)]),
            [],
        )
        self.assertEqual(missing["columns"]["color"]["source"], "missing")

    def test_required_column_publication_chain(self):
        registry = Registry()
        registry.store.upsert({"canonical_id": "P-1", "part_id": "A"})
        registry.store.upsert({"canonical_id": "P-2", "part_id": "B", "note": "KEEP"})
        self.assertTrue(registry.register(_note(1, "absent"), [])["ok"])
        jump = registry.register(
            _note(2, "public"),
            [{"op": "add_column", "name": "note"}],
        )
        self.assertIn("publication_jump", jump["reasons"])
        self.assertEqual(len(registry.versions), 1)
        self.assertTrue(
            registry.register(_note(2, "delete_only"), [{"op": "add_column", "name": "note"}])["ok"]
        )
        self.assertTrue(
            registry.register(
                _note(3, "write_only", has_default=True, default="UNBINNED"),
                [{"op": "set_default", "name": "note"}],
            )["ok"]
        )
        staged = ingest_bytes(registry, b"part_id\nA\n", writer_version=2, link=False)
        self.assertEqual(staged["status"], "accepted", staged["reason"])
        self.assertNotIn("note", staged["rows"][0]["cells"])
        early_reorg = registry.reorganize_drop("note")
        self.assertFalse(early_reorg["ok"])
        self.assertEqual(registry.store.rows["P-2"]["note"], "KEEP")
        early_public = registry.register(_note(4, "public", has_default=True, default="UNBINNED"), [])
        self.assertIn("backfill_required", early_public["reasons"])
        self.assertEqual(registry.latest().number, 3)
        filled = registry.backfill("note")
        self.assertTrue(filled["ok"], filled["reasons"])
        self.assertEqual(filled["filled"], 1)
        self.assertEqual(registry.store.rows["P-1"]["note"], "UNBINNED")
        self.assertEqual(registry.store.rows["P-2"]["note"], "KEEP")
        self.assertTrue(registry.register(_note(4, "public", has_default=True, default="UNBINNED"), [])["ok"])
        omitted = ingest_bytes(registry, b"part_id\nC\n", writer_version=4, link=False)
        self.assertEqual(omitted["reason"], "schema_resolution")
        self.assertEqual(omitted["rows"], [])
        written = ingest_bytes(registry, b"part_id,note\nC,BIN-1\n", writer_version=4, link=False)
        self.assertEqual(written["rows"][0]["cells"]["note"]["value"], "BIN-1")
        stale = ingest_bytes(registry, b"part_id\nA\n", writer_version=2, link=False)
        self.assertEqual(stale["reason"], "schema_too_old")
        self.assertEqual(stale["rows"], [])
        self.assertTrue(registry.register(_note(5, "write_only", has_default=True, default="UNBINNED"), [])["ok"])
        self.assertEqual(registry.store.rows["P-2"]["note"], "KEEP")
        self.assertTrue(registry.register(_note(6, "delete_only", has_default=True, default="UNBINNED"), [])["ok"])
        self.assertEqual(registry.store.rows["P-2"]["note"], "KEEP")
        early_drop = registry.register(_part(7), [{"op": "drop_column", "name": "note"}])
        self.assertIn("reorg_required", early_drop["reasons"])
        self.assertEqual(registry.latest().number, 6)
        self.assertTrue(registry.reorganize_drop("note")["ok"])
        self.assertNotIn("note", registry.store.rows["P-2"])
        self.assertNotIn("note", registry.store.rows["P-1"])
        dropped = registry.register(_part(7), [{"op": "drop_column", "name": "note"}])
        self.assertTrue(dropped["ok"], dropped["reasons"])
        self.assertEqual(registry.latest().number, 7)


if __name__ == "__main__":
    unittest.main()
