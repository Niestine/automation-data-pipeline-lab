import json
import unittest

import helpers  # noqa: F401

from api_reliability_lab.errors import SchemaError
from api_reliability_lab.schema import (
    parse_json,
    validate_ack_dict,
    validate_order_dict,
    validate_page_envelope,
    validate_webhook_dict,
)
from api_reliability_lab.seed import build_catalog, build_webhook_events


class SchemaTests(unittest.TestCase):
    def test_catalog_orders_are_valid(self):
        for row in build_catalog():
            self.assertEqual(validate_order_dict(row), [])

    def test_webhook_events_are_valid(self):
        for event in build_webhook_events():
            self.assertEqual(validate_webhook_dict(event), [])

    def test_rejects_unknown_status_and_extra_fields(self):
        row = dict(build_catalog()[0])
        row["status"] = "lost"
        errors = validate_order_dict(row)
        self.assertTrue(any("status" in item for item in errors))
        row = dict(build_catalog()[0])
        row["secret"] = "nope"
        errors = validate_order_dict(row)
        self.assertTrue(any("additional property" in item for item in errors))

    def test_rejects_amount_mismatch(self):
        row = dict(build_catalog()[0])
        row["items"] = [dict(item) for item in row["items"]]
        row["amount_cents"] = 1
        errors = validate_order_dict(row)
        self.assertTrue(any("line-item total" in item for item in errors))

    def test_rejects_bool_as_integer(self):
        row = dict(build_catalog()[0])
        row["version"] = True
        errors = validate_order_dict(row)
        self.assertTrue(any("version" in item for item in errors))

    def test_page_envelope_requires_cursor_when_has_more(self):
        errors = validate_page_envelope(
            {
                "object": "list",
                "items": [],
                "next_cursor": None,
                "has_more": True,
                "limit": 10,
            }
        )
        self.assertTrue(any("next_cursor" in item for item in errors))

    def test_page_envelope_null_cursor_when_complete(self):
        errors = validate_page_envelope(
            {
                "object": "list",
                "items": [],
                "next_cursor": "abc",
                "has_more": False,
                "limit": 10,
            }
        )
        self.assertTrue(any("next_cursor" in item for item in errors))

    def test_parse_json_rejects_nan_and_truncated_payloads(self):
        with self.assertRaises(SchemaError):
            parse_json('{"amount": NaN}')
        with self.assertRaises(SchemaError):
            parse_json('{"object":"list"')
        with self.assertRaises(SchemaError):
            parse_json(b"\xff\xfe")

    def test_ack_schema(self):
        self.assertEqual(
            validate_ack_dict({"order_id": "ORD-1001", "version": 1, "action": "ack_shipment"}),
            [],
        )
        errors = validate_ack_dict({"order_id": "ORD-1001", "version": 1, "action": "delete"})
        self.assertTrue(errors)

    def test_cancelled_event_must_carry_cancelled_status(self):
        event = build_webhook_events()[-1]
        event = json.loads(json.dumps(event))
        event["data"]["status"] = "paid"
        errors = validate_webhook_dict(event)
        self.assertTrue(any("cancelled" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
