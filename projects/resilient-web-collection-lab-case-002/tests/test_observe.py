import helpers  # noqa: F401
import unittest

from incremental_crawl_lab.config import Config
from incremental_crawl_lab.corpus import BASE, NOT_FOUND
from incremental_crawl_lab.observe import bind_soft_hashes, etags_match, interpret


class ObserveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(simhash_k=23, material_detection=True)
        bind_soft_hashes(self.config)

    def test_weak_validator_does_not_certify_a_later_304(self) -> None:
        body = BASE.encode("utf-8")
        first = interpret(
            {},
            200,
            {"Content-Type": "text/html; charset=utf-8", "ETag": 'W/"v1"'},
            body,
            self.config,
        )
        self.assertEqual(first.kind, "baseline")
        self.assertFalse(first.byte_identity_known)
        stored = {
            "sha256": first.columns["sha256"],
            "simhash": first.columns["simhash"],
            "etag": first.columns["etag"],
            "weak": first.columns["weak"],
            "observation_count": 1,
        }
        second = interpret(stored, 304, {"ETag": 'W/"v1"'}, None, self.config)
        self.assertEqual(second.kind, "validator_not_modified")
        self.assertFalse(second.oracle_sync)
        self.assertFalse(second.counts_observation)
        self.assertEqual(second.material_delta, 0)
        self.assertFalse(second.byte_identity_known)
        self.assertTrue(second.updates_sync)
        self.assertTrue(second.charges_page)

    def test_strong_304_is_a_no_change_sample(self) -> None:
        body = BASE.encode("utf-8")
        first = interpret(
            {},
            200,
            {"Content-Type": "text/html; charset=utf-8", "ETag": '"v1"'},
            body,
            self.config,
        )
        self.assertTrue(first.byte_identity_known)
        stored = {
            "sha256": first.columns["sha256"],
            "simhash": first.columns["simhash"],
            "etag": first.columns["etag"],
            "weak": 0,
            "observation_count": 1,
        }
        second = interpret(stored, 304, {"ETag": '"v1"'}, None, self.config)
        self.assertTrue(second.oracle_sync)
        self.assertTrue(second.counts_observation)
        self.assertEqual(second.material_delta, 0)
        self.assertTrue(second.byte_identity_known)
        self.assertTrue(etags_match('W/"v1"', '"v1"'))

    def test_soft_error_is_not_a_material_update(self) -> None:
        body = NOT_FOUND.encode("utf-8")
        decision = interpret(
            {"sha256": "different", "simhash": "0000000000000001", "etag": None, "weak": 0},
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            body,
            self.config,
        )
        self.assertEqual(decision.kind, "soft_error")
        self.assertEqual(decision.material_delta, 0)
        self.assertEqual(decision.byte_delta, 1)
        self.assertFalse(decision.counts_observation)

    def test_missing_charset_keeps_the_checksum_and_drops_simhash(self) -> None:
        body = b"<html><head><meta charset=\"utf-8\"></head><body><p>Cafe</p></body></html>"
        decision = interpret({}, 200, {"Content-Type": "text/html"}, body, self.config)
        self.assertIsNone(decision.columns["simhash"])
        self.assertEqual(decision.columns["charset_unknown"], 1)
        self.assertEqual(len(decision.columns["sha256"]), 64)
        self.assertTrue(decision.charset_unknown)

    def test_body_that_does_not_decode_with_the_declared_charset(self) -> None:
        body = b"<html><body><p>caf\xe9</p></body></html>"
        decision = interpret({}, 200, {"Content-Type": "text/html; charset=utf-8"}, body, self.config)
        self.assertIsNone(decision.columns["simhash"])
        self.assertEqual(decision.columns["charset_unknown"], 1)
        self.assertIsNone(decision.columns["charset"])
        self.assertTrue(decision.charset_unknown)


if __name__ == "__main__":
    unittest.main()
