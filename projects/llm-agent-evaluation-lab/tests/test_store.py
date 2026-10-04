import unittest

import helpers
from llm_agent_lab.models import CONTRACT_VERSION, RunResult, RunStatus
from llm_agent_lab.store import RunStore


def _result(ticket, status=RunStatus.COMPLETED):
    return RunResult(
        ticket_id=ticket.ticket_id,
        status=status,
        contract_version=CONTRACT_VERSION,
        input_hash=ticket.input_hash(),
        run_id="abc",
        attempts=1,
    )


class StoreTests(unittest.TestCase):
    def test_same_ticket_hits_cache(self):
        store = RunStore()
        ticket = helpers.make_ticket()
        store.save(ticket, _result(ticket), dry_run=False, approve=False)
        hit = store.get(ticket, dry_run=False, approve=False)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.cached)
        self.assertEqual(hit.status, RunStatus.COMPLETED)

    def test_cached_copy_does_not_mutate_store(self):
        store = RunStore()
        ticket = helpers.make_ticket()
        store.save(ticket, _result(ticket), dry_run=False, approve=False)
        hit = store.get(ticket, dry_run=False, approve=False)
        hit.status = RunStatus.FAILED
        again = store.get(ticket, dry_run=False, approve=False)
        self.assertEqual(again.status, RunStatus.COMPLETED)

    def test_different_body_is_different_key(self):
        store = RunStore()
        first = helpers.make_ticket(body="lookup ORD-100")
        second = helpers.make_ticket(body="lookup ORD-100 please")
        store.save(first, _result(first), dry_run=False, approve=False)
        self.assertIsNone(store.get(second, dry_run=False, approve=False))
        self.assertNotEqual(first.input_hash(), second.input_hash())

    def test_dry_run_and_approve_are_distinct_keys(self):
        store = RunStore()
        ticket = helpers.make_ticket()
        store.save(ticket, _result(ticket, RunStatus.DRY_RUN), dry_run=True, approve=False)
        self.assertIsNone(store.get(ticket, dry_run=False, approve=False))
        self.assertIsNone(store.get(ticket, dry_run=True, approve=True))
        self.assertIsNotNone(store.get(ticket, dry_run=True, approve=False))


if __name__ == "__main__":
    unittest.main()
