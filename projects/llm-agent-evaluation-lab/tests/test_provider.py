import json
import unittest

import helpers
from llm_agent_lab.models import CompletionRequest
from llm_agent_lab.provider import FakeProvider, HeuristicProvider, ProviderError


def _request(task_id="T-1001", user=None):
    return CompletionRequest(
        task_id=task_id,
        system="sys",
        user=user or helpers.make_ticket().canonical_json(),
    )


class FakeProviderTests(unittest.TestCase):
    def test_scripted_sequence_then_repeats_last_step(self):
        provider = FakeProvider(
            {
                "T-R1": [
                    {"error": True, "transient": True, "code": "timeout", "message": "timeout"},
                    {"text": helpers.valid_output_json()},
                ]
            }
        )
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(_request("T-R1"))
        self.assertTrue(ctx.exception.transient)
        second = provider.complete(_request("T-R1"))
        third = provider.complete(_request("T-R1"))
        self.assertEqual(second.text, third.text)
        self.assertEqual(len(provider.call_log), 3)

    def test_missing_script_raises(self):
        provider = FakeProvider({})
        with self.assertRaises(ProviderError) as ctx:
            provider.complete(_request("missing"))
        self.assertEqual(ctx.exception.code, "no_script")


class HeuristicProviderTests(unittest.TestCase):
    def test_lookup_classification_is_deterministic(self):
        provider = HeuristicProvider()
        ticket = helpers.make_ticket()
        first = json.loads(provider.complete(_request(ticket.ticket_id, ticket.canonical_json())).text)
        second = json.loads(provider.complete(_request(ticket.ticket_id, ticket.canonical_json())).text)
        self.assertEqual(first, second)
        self.assertEqual(first["intent"], "status_lookup")
        self.assertEqual(first["action_args"]["record_id"], "ORD-100")

    def test_update_and_export_and_escalate(self):
        provider = HeuristicProvider()
        update = helpers.make_ticket(
            ticket_id="T-U",
            subject="Update ORD-200",
            body="Please update ORD-200 and set status to shipped.",
        )
        export = helpers.make_ticket(
            ticket_id="T-E",
            body="Export aggregate counts of orders in the lab store.",
        )
        escalate = helpers.make_ticket(
            ticket_id="T-S",
            body="Escalate ORD-300 to the manager on-call.",
        )
        update_out = json.loads(provider.complete(_request("T-U", update.canonical_json())).text)
        export_out = json.loads(provider.complete(_request("T-E", export.canonical_json())).text)
        escalate_out = json.loads(provider.complete(_request("T-S", escalate.canonical_json())).text)
        self.assertEqual(update_out["proposed_action"], "update_record")
        self.assertEqual(update_out["action_args"]["fields"]["status"], "shipped")
        self.assertEqual(export_out["proposed_action"], "export_data")
        self.assertEqual(export_out["action_args"]["scope"], "aggregate_counts")
        self.assertEqual(escalate_out["proposed_action"], "escalate")

    def test_unknown_falls_back_to_refuse(self):
        provider = HeuristicProvider()
        ticket = helpers.make_ticket(ticket_id="T-Z", subject="Hello", body="Just saying hi.")
        payload = json.loads(provider.complete(_request("T-Z", ticket.canonical_json())).text)
        self.assertEqual(payload["proposed_action"], "refuse")
        self.assertEqual(payload["intent"], "unknown")


if __name__ == "__main__":
    unittest.main()
