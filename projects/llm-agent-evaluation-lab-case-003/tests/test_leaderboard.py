"""Retriever ranking is decisive. Generator order and overlap are not."""

import unittest

import helpers
from rag_eval_lab.errors import LabError
from rag_eval_lab.generator import TerseGenerator, TrustingGenerator
from rag_eval_lab.leaderboard import assert_round_trip, generator_board, rank_generators, select_operating_point
from rag_eval_lab.retrieve import Hit, LexicalRetriever
from rag_eval_lab.schema_gate import validate_response
from rag_eval_lab.sweep import build_chunks, load_spec, run_metric_sweep

from helpers import EXAMPLES


class LeaderboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sweep = run_metric_sweep(load_spec(EXAMPLES / "sweep_tokens.json"))

    def test_frozen_generator_sweep_fractions(self) -> None:
        by_id = self.sweep["by_id"]
        expected = {
            "k1-s1-o0-f0.0": {"claim_recall": 0.25, "faithfulness": 0.5, "relevant_noise": 0.0, "f1": 1 / 3, "context_precision": 1.0, "precision": 1 / 2, "recall": 1 / 4},
            "k2-s1-o0-f0.0": {"claim_recall": 0.5, "faithfulness": 0.8, "relevant_noise": 0.4, "f1": 4 / 9, "context_precision": 1.0, "precision": 2 / 5, "recall": 0.5},
            "k4-s1-o0-f0.0": {"claim_recall": 1.0, "faithfulness": 10 / 11, "relevant_noise": 6 / 11, "f1": 8 / 15, "context_precision": 1.0, "precision": 4 / 11, "recall": 1.0},
            "k5-s1-o0-f0.0": {"claim_recall": 1.0, "faithfulness": 10 / 11, "relevant_noise": 6 / 11, "f1": 8 / 15, "context_precision": 0.8, "precision": 4 / 11, "recall": 1.0},
            "k1-s2-o0-f0.0": {"claim_recall": 0.5, "faithfulness": 0.8, "relevant_noise": 0.4, "f1": 4 / 9, "context_precision": 1.0},
            "k4-s2-o0-f0.0": {"claim_recall": 1.0, "faithfulness": 10 / 11, "relevant_noise": 6 / 11, "f1": 8 / 15, "context_precision": 2 / 3},
            "k4-s2-o1-f0.0": {"claim_recall": 1.0, "context_precision": 1.0},
            "k5-s1-o0-f0.95": {"claim_recall": 0.25, "faithfulness": 0.5, "relevant_noise": 0.0, "f1": 1 / 3, "context_precision": 1.0},
        }
        for config_id, fields in expected.items():
            row = by_id[config_id]
            for key, value in fields.items():
                self.assertAlmostEqual(row[key], value, places=6, msg=f"{config_id} {key}")
        wide = by_id["k1-s2-o0-f0.0"]
        narrow = by_id["k1-s1-o0-f0.0"]
        self.assertGreater(wide["claim_recall"], narrow["claim_recall"])
        self.assertGreater(wide["faithfulness"], narrow["faithfulness"])
        self.assertGreater(wide["relevant_noise"], narrow["relevant_noise"])
        self.assertAlmostEqual(by_id["k4-s2-o0-f0.0"]["claim_recall"], by_id["k4-s2-o1-f0.0"]["claim_recall"])
        self.assertNotAlmostEqual(by_id["k4-s2-o0-f0.0"]["context_precision"], by_id["k4-s2-o1-f0.0"]["context_precision"])
        gain_early = by_id["k2-s1-o0-f0.0"]["f1"] - by_id["k1-s1-o0-f0.0"]["f1"]
        gain_late = by_id["k4-s1-o0-f0.0"]["f1"] - by_id["k2-s1-o0-f0.0"]["f1"]
        self.assertAlmostEqual(gain_early, 1 / 9)
        self.assertLess(gain_late, gain_early)

    def test_retriever_board_is_decisive_and_ignores_context_precision(self) -> None:
        board = self.sweep["leaderboard"]
        self.assertTrue(board["decisive"])
        self.assertEqual(board["sort_keys"], ["claim_recall", "gold_chunk_hit_rate"])
        self.assertIn("context_precision", board["diagnostic_columns"])
        self.assertEqual(board["rows"][0]["config_id"], "k4-s1-o0-f0.0")
        order = [row["config_id"] for row in board["rows"]]
        self.assertLess(order.index("k5-s1-o0-f0.0"), order.index("k1-s1-o0-f0.0"))
        self.assertGreater(
            self.sweep["by_id"]["k1-s1-o0-f0.0"]["context_precision"],
            self.sweep["by_id"]["k5-s1-o0-f0.0"]["context_precision"],
        )
        point = self.sweep["operating_point"]
        self.assertEqual(point["config_id"], "k4-s1-o0-f0.0")
        self.assertEqual(point["overlap"], 0)
        self.assertFalse(point["overlap_tuned"])
        self.assertFalse(self.sweep["overlap_tuned"])

    def test_operating_point_keeps_the_overlap_zero_slice(self) -> None:
        rows = [
            {"config_id": "overlap-high", "overlap": 1, "claim_recall": 1.0, "gold_chunk_hit_rate": 1.0},
            {"config_id": "overlap-zero", "overlap": 0, "claim_recall": 0.5, "gold_chunk_hit_rate": 1.0},
        ]
        chosen = select_operating_point(rows)
        self.assertEqual(chosen["config_id"], "overlap-zero")
        self.assertFalse(chosen["overlap_tuned"])

    def test_computed_generator_board_has_no_winner(self) -> None:
        board = self.sweep["generator_comparison"]
        self.assertFalse(board["decisive"])
        self.assertIsNone(board["winner"])
        self.assertEqual(board["sort_keys"], [])
        terse, trusting = board["rows"]
        self.assertEqual(terse["generator_id"], "terse-v1")
        self.assertEqual(trusting["generator_id"], "trusting-v1")
        self.assertEqual({terse["config_id"], trusting["config_id"]}, {self.sweep["operating_point"]["config_id"]})
        expected_terse = {"faithfulness": 1.0, "citation_recall": 1.0, "precision": 1.0, "recall": 0.25, "f1": 0.4, "hallucination": 0.0}
        expected_trusting = {"faithfulness": 10 / 11, "citation_recall": 10 / 11, "precision": 4 / 11, "recall": 1.0, "f1": 8 / 15, "hallucination": 1 / 11}
        for row, expected in ((terse, expected_terse), (trusting, expected_trusting)):
            for key, value in expected.items():
                self.assertAlmostEqual(row[key], value, places=6, msg=f"{row['generator_id']} {key}")
        # Each generator leads on a different column, so no single sort key is honest.
        self.assertGreater(terse["faithfulness"], trusting["faithfulness"])
        self.assertGreater(trusting["recall"], terse["recall"])

    def test_frozen_generators_emit_valid_payloads(self) -> None:
        spec = load_spec(EXAMPLES / "sweep_tokens.json")
        _chunks, evidences, _gold = build_chunks(spec, 1, 0)
        retrieved = self.sweep["by_id"]["k2-s1-o0-f0.0"]["retrieved"]
        hits = [Hit(chunk_id, 1.0, "", ()) for chunk_id in retrieved]
        for generator in (TerseGenerator(evidences), TrustingGenerator(evidences)):
            payload = generator.complete("sweep", 0, hits, "")
            self.assertEqual(validate_response(payload), [], generator.provider_id)
        empty = TerseGenerator(evidences).complete("sweep", 0, [], "")
        self.assertEqual(validate_response(empty), [])
        self.assertEqual(empty["claims"][0]["kind"], "insufficient_evidence")

    def test_generator_order_and_round_trip_id(self) -> None:
        board = generator_board(
            [
                {"generator_id": "terse-v1", "faithfulness": 0.0},
                {"generator_id": "trusting-v1", "faithfulness": 0.9},
            ]
        )
        self.assertFalse(board["decisive"])
        self.assertIsNone(board["winner"])
        self.assertEqual([row["generator_id"] for row in board["rows"]], ["terse-v1", "trusting-v1"])
        with self.assertRaises(LabError):
            rank_generators(board["rows"])
        with self.assertRaises(LabError):
            assert_round_trip(LexicalRetriever.retriever_id, LexicalRetriever.retriever_id)
        assert_round_trip(LexicalRetriever.retriever_id, None)
        assert_round_trip(LexicalRetriever.retriever_id, "dense-v2")


if __name__ == "__main__":
    unittest.main()
