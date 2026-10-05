"""Hand-built histories for the dependency checker."""

from __future__ import annotations

import json
import unittest

from helpers import ROOT

from prefixlab.history import check_history, parse_history


class CheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.histories = json.loads((ROOT / "examples" / "histories.json").read_text(encoding="utf-8"))

    def test_fixtures(self):
        expected = {
            "serial": None,
            "g1a": "G1a",
            "g1b": "G1b",
            "g0": "G0",
            "g1c": "G1c",
            "g2": "G2",
            "unknown": None,
        }
        for name, anomaly in expected.items():
            report = check_history(self.histories[name])
            self.assertEqual(report["anomaly"], anomaly, name)
        g1a = check_history(self.histories["g1a"])
        self.assertEqual(set(g1a["transactions"]), {"T1", "T2"})
        g0 = check_history(self.histories["g0"])
        self.assertEqual(set(g0["transactions"]), {"T1", "T2"})
        g2 = check_history(self.histories["g2"])
        self.assertEqual(set(g2["transactions"]), {"T1", "T2"})
        self.assertTrue(any(edge["kind"] == "anti-depends" for edge in g2["edges"]))
        self.assertEqual({edge["kind"] for edge in g0["edges"]}, {"write-depends"})
        g1b = check_history(self.histories["g1b"])
        self.assertEqual(set(g1b["transactions"]), {"T1", "T2"})
        g1c = check_history(self.histories["g1c"])
        self.assertEqual(set(g1c["transactions"]), {"T1", "T2"})
        self.assertIn("read-depends", {edge["kind"] for edge in g1c["edges"]})
        self.assertNotIn("anti-depends", {edge["kind"] for edge in g1c["edges"]})
        unknown = check_history(self.histories["unknown"])
        self.assertEqual(unknown["missing"], [])
        self.assertEqual(unknown["edges"], [])

    def test_extra_anti_edge_does_not_upgrade_a_g0_cycle(self):
        history = json.loads(json.dumps(self.histories["g0"]))
        history["transactions"][0]["ops"].insert(0, {"op": "read", "key": "y", "value": []})
        report = check_history(history)
        self.assertEqual(report["anomaly"], "G0")
        self.assertEqual({edge["kind"] for edge in report["edges"]}, {"write-depends"})
        self.assertEqual(len(report["edges"]), 2)

    def test_lost_append_without_a_cycle_names_the_writer(self):
        history = {
            "transactions": [
                {"id": "T1", "status": "committed", "ops": [
                    {"op": "append", "key": "x", "tokens": ["a"], "value": ["a"]}]},
            ],
            "surviving": {"x": []},
        }
        report = check_history(history)
        self.assertEqual(report["anomaly"], "lost_append")
        self.assertEqual(report["missing"], ["a"])
        self.assertEqual(report["transactions"], ["T1"])

    def test_malformed_history_is_refused(self):
        with self.assertRaises(ValueError):
            parse_history({"transactions": [{"id": "T1", "status": "maybe", "ops": []}]})
        with self.assertRaises(ValueError):
            parse_history({"transactions": "nope"})


if __name__ == "__main__":
    unittest.main()
