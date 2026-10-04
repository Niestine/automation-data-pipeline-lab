import unittest

import helpers
from brief_router_lab.bindings import BindError, resolve_bind_map, resolve_expr, step_id_from_expr


class BindingTests(unittest.TestCase):
    def test_step_id_extracted(self):
        self.assertEqual(step_id_from_expr("$s1.hits[0].asset_id"), "s1")

    def test_nested_index_path(self):
        completed = {"s1": {"hits": [{"asset_id": "AST-101"}, {"asset_id": "AST-102"}]}}
        self.assertEqual(resolve_expr("$s1.hits[0].asset_id", completed), "AST-101")
        self.assertEqual(resolve_expr("$s1.hits[1].asset_id", completed), "AST-102")

    def test_missing_index_is_bind_path(self):
        completed = {"s1": {"hits": []}}
        with self.assertRaises(BindError) as ctx:
            resolve_expr("$s1.hits[0].asset_id", completed)
        self.assertEqual(ctx.exception.code, "bind_path")

    def test_unresolved_step(self):
        with self.assertRaises(BindError) as ctx:
            resolve_expr("$s2.draft_id", {"s1": {"draft_id": "DRF-1"}})
        self.assertEqual(ctx.exception.code, "bind_unresolved")

    def test_bad_syntax(self):
        with self.assertRaises(BindError) as ctx:
            step_id_from_expr("s1.draft_id")
        self.assertEqual(ctx.exception.code, "bind_syntax")

    def test_resolve_map(self):
        completed = {"s3": {"draft_id": "DRF-P-2002"}}
        merged = resolve_bind_map({"draft_id": "$s3.draft_id"}, completed)
        self.assertEqual(merged["draft_id"], "DRF-P-2002")

    def test_helpers_packet_id(self):
        self.assertEqual(helpers.make_packet().packet_id, "P-2001")
