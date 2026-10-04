import tempfile
import unittest

import helpers  # noqa: F401
from helpers import catalog_dict, job_dict, make_runner

from automation_job_lab.lease import LeaseStore
from automation_job_lab.models import LAB_EPOCH_MS
from automation_job_lab.runner import lease_ttl_ms
from automation_job_lab.schema import load_catalog


class LeaseTests(unittest.TestCase):
    def test_acquire_and_release(self):
        store = LeaseStore()
        self.assertTrue(store.acquire("ingest-inbox", "run-a", LAB_EPOCH_MS, 1_000))
        self.assertFalse(store.acquire("ingest-inbox", "run-b", LAB_EPOCH_MS, 1_000))
        self.assertTrue(store.release("ingest-inbox", "run-a"))
        self.assertTrue(store.acquire("ingest-inbox", "run-b", LAB_EPOCH_MS, 1_000))

    def test_same_holder_reacquires(self):
        store = LeaseStore()
        self.assertTrue(store.acquire("ingest-inbox", "run-a", LAB_EPOCH_MS, 1_000))
        self.assertTrue(store.acquire("ingest-inbox", "run-a", LAB_EPOCH_MS + 10, 1_000))
        lease = store.get("ingest-inbox")
        self.assertEqual(lease.expires_ms, LAB_EPOCH_MS + 10 + 1_000)

    def test_expired_lease_can_be_stolen(self):
        store = LeaseStore()
        self.assertTrue(store.acquire("ingest-inbox", "run-a", LAB_EPOCH_MS, 50))
        self.assertTrue(store.acquire("ingest-inbox", "run-b", LAB_EPOCH_MS + 51, 50))
        self.assertEqual(store.get("ingest-inbox").holder, "run-b")

    def test_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/leases.json"
            store = LeaseStore(path)
            store.acquire("ingest-inbox", "run-a", LAB_EPOCH_MS, 1_000)
            loaded = LeaseStore(path)
            self.assertEqual(loaded.get("ingest-inbox").holder, "run-a")

    def test_held_lease_skips_the_job_and_dependents(self):
        catalog = catalog_dict(
            job_dict(
                id="ingest-inbox",
                handler="ingest",
                params={"source": "inbox", "dest": "staging"},
            ),
            job_dict(
                id="transform-records",
                handler="transform",
                depends_on=["ingest-inbox"],
                params={"source": "staging", "dest": "clean", "dead_letter": "dead_letter"},
            ),
            job_dict(id="heartbeat-log"),
        )
        runner, _, _, logger, _, _, leases = make_runner(catalog=catalog, faults=[])
        leases.acquire("ingest-inbox", "other-run", LAB_EPOCH_MS, 60_000)
        report = runner.run()
        jobs = report.job_map()
        self.assertEqual(jobs["heartbeat-log"].status, "succeeded")
        self.assertEqual(jobs["ingest-inbox"].status, "skipped")
        self.assertEqual(jobs["ingest-inbox"].reason, "leased")
        self.assertEqual(jobs["transform-records"].status, "skipped")
        self.assertEqual(jobs["transform-records"].reason, "dependency_skipped")
        self.assertEqual(logger.of_type("lease_held")[0]["job_id"], "ingest-inbox")

    def test_lease_ttl_covers_every_attempt_and_backoff(self):
        job = load_catalog(catalog_dict(job_dict(timeout_ms=1000))).jobs[0]
        # 4 attempts * 1000 ms + 3 retries * (200 ms cap + 3 ms jitter) + 100 ms margin.
        self.assertEqual(lease_ttl_ms(job), 4_000 + 3 * 203 + 100)
