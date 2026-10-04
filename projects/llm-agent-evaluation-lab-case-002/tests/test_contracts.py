import json
import math
import unittest

import helpers
from brief_router_lab.contracts import (
    ContractError,
    extract_json_object,
    parse_tool_plan,
    validate_schema,
    TOOL_PLAN_SCHEMA,
)
from brief_router_lab.registry import CATALOG_GET_SCHEMA, CATALOG_SEARCH_SCHEMA


class ContractTests(unittest.TestCase):
    def test_valid_plan_parses(self):
        plan = parse_tool_plan(helpers.valid_plan_json())
        self.assertEqual(plan.goal_kind, "lookup")
        self.assertEqual(plan.steps[0].tool, "catalog.search")
        self.assertEqual(plan.budget_tokens, 20)

    def test_markdown_fence_extracted(self):
        body = helpers.valid_plan_json()
        fenced = f"```json\n{body}\n```"
        plan = parse_tool_plan(fenced)
        self.assertEqual(plan.schema, "tool_plan_v1")

    def test_empty_response_is_parse_error(self):
        with self.assertRaises(ContractError) as ctx:
            parse_tool_plan("   ")
        self.assertEqual(ctx.exception.code, "parse_error")

    def test_nan_rejected(self):
        blob = helpers.valid_plan_dict()
        raw = json.dumps(blob).replace("0.91", "NaN")
        with self.assertRaises(ContractError) as ctx:
            parse_tool_plan(raw)
        self.assertEqual(ctx.exception.code, "parse_error")
        self.assertTrue(math.isnan(float("nan")))

    def test_extra_property_rejected(self):
        blob = helpers.valid_plan_dict(comment="nope")
        with self.assertRaises(ContractError) as ctx:
            parse_tool_plan(json.dumps(blob))
        self.assertEqual(ctx.exception.code, "schema_error")
        self.assertTrue(any("comment" in item for item in ctx.exception.errors))

    def test_unknown_goal_kind_rejected(self):
        blob = helpers.valid_plan_dict(goal_kind="ticket_triage")
        with self.assertRaises(ContractError) as ctx:
            parse_tool_plan(json.dumps(blob))
        self.assertEqual(ctx.exception.code, "schema_error")

    def test_empty_steps_rejected(self):
        blob = helpers.valid_plan_dict(steps=[])
        errors = validate_schema(TOOL_PLAN_SCHEMA, blob)
        self.assertTrue(any("minItems" in item for item in errors))

    def test_tool_arg_pattern(self):
        errors = validate_schema(CATALOG_GET_SCHEMA, {"asset_id": "clip-1"})
        self.assertTrue(errors)
        ok = validate_schema(CATALOG_GET_SCHEMA, {"asset_id": "AST-101"})
        self.assertEqual(ok, [])

    def test_search_limit_bounds(self):
        errors = validate_schema(CATALOG_SEARCH_SCHEMA, {"query": "x", "limit": 0})
        self.assertTrue(errors)
        errors = validate_schema(CATALOG_SEARCH_SCHEMA, {"query": "x", "limit": 3.5})
        self.assertTrue(any("integer" in item for item in errors))

    def test_extract_requires_object(self):
        with self.assertRaises(ContractError):
            extract_json_object("no object here")
