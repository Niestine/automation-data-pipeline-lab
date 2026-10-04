import unittest

from brief_router_lab.models import CONTRACT_VERSION, RunResult, RunStatus
from brief_router_lab.store import RunStore, StepStore, step_key
from brief_router_lab.models import StepResult
import helpers


class StoreTests(unittest.TestCase):
    def test_live_and_dry_keys_differ(self):
        packet = helpers.make_packet()
        store = RunStore()
        live = store.key(packet, dry_run=False, approve=False)
        dry = store.key(packet, dry_run=True, approve=False)
        approved = store.key(packet, dry_run=False, approve=True)
        self.assertNotEqual(live, dry)
        self.assertNotEqual(live, approved)
        self.assertIn(CONTRACT_VERSION, live)

    def test_get_marks_cached(self):
        packet = helpers.make_packet()
        store = RunStore()
        result = RunResult(
            packet_id=packet.packet_id,
            status=RunStatus.COMPLETED,
            contract_version=CONTRACT_VERSION,
            input_hash=packet.input_hash(),
            run_id="abc",
            attempts=1,
        )
        store.save(packet, result, dry_run=False, approve=False)
        hit = store.get(packet, dry_run=False, approve=False)
        self.assertTrue(hit.cached)
        self.assertFalse(result.cached)

    def test_step_key_includes_args_and_mode(self):
        packet = helpers.make_packet(packet_id="P-1")
        a = step_key(packet, "s1", {"asset_id": "AST-101"}, dry_run=False)
        b = step_key(packet, "s1", {"asset_id": "AST-102"}, dry_run=False)
        c = step_key(packet, "s1", {"asset_id": "AST-101"}, dry_run=True)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, step_key(packet, "s1", {"asset_id": "AST-101"}, dry_run=False))

    def test_step_key_differs_by_workspace_for_same_packet_id(self):
        public = helpers.make_packet(packet_id="P-1", workspace="public")
        restricted = helpers.make_packet(packet_id="P-1", workspace="restricted")
        args = {"query": "launch", "limit": 3}
        self.assertNotEqual(
            step_key(public, "s1", args, dry_run=False),
            step_key(restricted, "s1", args, dry_run=False),
        )

    def test_step_store_replays(self):
        store = StepStore()
        result = StepResult("s1", "catalog.get", "completed", {"found": True})
        store.save("k", result)
        hit = store.get("k")
        self.assertTrue(hit.replayed)
        self.assertEqual(hit.payload["found"], True)
