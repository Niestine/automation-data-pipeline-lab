"""Cassette episodes: modes, ablations, lanes, refusals, and noisy tests."""

from __future__ import annotations

import logging
import unittest

from helpers import outcome, result
from repair_gate.harness import CassetteProvider, EpisodeConfig, run_episode
from repair_gate.util import canonical


class HarnessTests(unittest.TestCase):
    def test_modes_keep_format_and_answer_apart(self):
        modes = outcome()["report"]["modes"]
        self.assertEqual(modes["reasoning"]["strict"], {"format_success": True, "task_exact_match": True})
        self.assertEqual(modes["reasoning"]["fri"], {"format_success": False, "task_exact_match": True})
        self.assertEqual(modes["reasoning"]["nl_to_format"], {"format_success": True, "task_exact_match": True})
        self.assertEqual(modes["classification"]["strict"], {"format_success": True, "task_exact_match": True})
        self.assertEqual(modes["classification"]["fri"], {"format_success": True, "task_exact_match": False})
        self.assertEqual(modes["classification"]["nl_to_format"], {"format_success": True, "task_exact_match": True})
        natural = result("ep-reason-nl")
        self.assertEqual(natural.trace_text, "18 plus 24 is 42.")
        self.assertNotIn("18 plus 24", canonical(natural.prompts))
        self.assertEqual(natural.candidate["answer"], "42")

    def test_key_order_follows_the_schema(self):
        order = outcome()["report"]["key_order"]
        self.assertEqual(order["default"], ["rationale", "answer"])
        self.assertEqual(order["swapped"], ["answer", "rationale"])

    def test_ablation_table(self):
        ablation = outcome()["report"]["ablation"]
        self.assertEqual(ablation["raw"]["schema_pass_rate"], 0.0)
        self.assertEqual(ablation["raw"]["leaf_accuracy"], 0.0)
        self.assertEqual(ablation["raw"]["outside_rate"], 1.0)
        self.assertEqual(ablation["raw"]["mean_calls"], 3.0)
        for name in ("loc_obs", "full_prose", "full_keyed"):
            self.assertEqual(ablation[name]["outside_rate"], 0.0)
            self.assertEqual(ablation[name]["mean_calls"], 2.0)
        self.assertEqual(ablation["loc_obs"]["schema_pass_rate"], 0.0)
        self.assertEqual(ablation["full_prose"]["schema_pass_rate"], 1.0)
        self.assertEqual(ablation["full_keyed"]["leaf_accuracy"], 1.0)
        raw = result("ep-ablate-code-raw")
        loc = result("ep-ablate-code-loc")
        prose = result("ep-ablate-code-prose")
        keyed = result("ep-ablate-code-keyed")
        self.assertIsNone(raw.prompts[0]["repair"])
        self.assertEqual(raw.prompts[1]["repair"], 'enum failed at /code observed "RQ-99"')
        self.assertNotIn("alternatives", loc.prompts[1]["repair"])
        self.assertIn('["RQ-14","RQ-22"]', prose.prompts[1]["repair"])
        self.assertEqual(keyed.prompts[1]["repair"]["alternatives"], ["RQ-14", "RQ-22"])
        self.assertEqual(keyed.prompts[1]["repair"]["location"], prose.prompts[1]["repair"].split("at ")[1].split(".")[0])

    def test_lanes_bind_different_budgets(self):
        lanes = outcome()["report"]["lanes"]
        self.assertEqual(lanes["A"]["status"], "budget_patch")
        self.assertFalse(lanes["A"]["schema_valid"])
        self.assertEqual(lanes["B"]["status"], "valid")
        self.assertTrue(lanes["B"]["schema_valid"])
        lane_a = result("ep-lane__laneA")
        lane_b = result("ep-lane__laneB")
        self.assertEqual(lane_a.model_calls, 3)
        self.assertEqual(lane_a.patch_attempts, 2)
        self.assertEqual(lane_a.candidate["arguments"]["currency"], "USD")
        self.assertEqual(lane_b.model_calls, 4)
        self.assertEqual(lane_b.candidate["arguments"]["currency"], "JPY")
        self.assertEqual(lane_a.prompts[2]["repair"]["label"], "mask.missing_test")
        self.assertEqual(lane_a.prompts[2]["repair"]["observed"], "USD")
        self.assertEqual(lane_a.prompts[2]["repair"]["alternatives"], [])

    def test_refusal_truncation_and_parse_holes(self):
        refusal = result("ep-refusal")
        self.assertEqual(refusal.status, "refusal")
        self.assertEqual(refusal.patch_attempts, 0)
        self.assertFalse(refusal.executed)
        self.assertEqual(refusal.unsafe_dispatch, 0)
        self.assertIsNone(refusal.candidate)
        truncated = result("ep-truncated")
        self.assertEqual(truncated.patch_attempts, 0)
        self.assertEqual(truncated.ops_applied, [])
        self.assertEqual(truncated.candidate, {"code": "RQ-14", "lane": "domestic"})
        self.assertEqual(truncated.manifest["payload"]["events"][0]["raw"]["status"], "truncated")
        hole = result("ep-json-hole")
        self.assertEqual(hole.repair_records[0]["label"], "required")
        self.assertIn("lane", hole.repair_records[0]["alternatives"])
        self.assertEqual(hole.patch_attempts, 1)
        broken = result("ep-unparseable")
        self.assertEqual(broken.repair_records[0]["label"], "json.parse")
        self.assertEqual(broken.repair_records[0]["location"], "")
        self.assertEqual(broken.repair_records[0]["alternatives"], [])
        self.assertFalse(broken.repair_records[0]["observed_present"])

    def test_noisy_verifier_fails_at_apply_and_keeps_the_object(self):
        noisy = result("ep-noisy")
        self.assertEqual(noisy.candidate["code"], "RQ-99")
        self.assertEqual(noisy.prompts[1]["repair"]["observed"], "NOISE")
        self.assertEqual(noisy.prompts[1]["repair"]["alternatives"], ["RQ-14", "RQ-22"])
        self.assertEqual(noisy.prompts[2]["repair"]["label"], "apply.test_mismatch")
        self.assertEqual(noisy.prompts[2]["repair"]["observed"], "NOISE")
        self.assertEqual(noisy.prompts[2]["repair"]["location"], "/code")
        apply_events = [item for item in noisy.manifest["payload"]["events"] if item["kind"] == "apply"]
        self.assertEqual(apply_events[0]["reason"], "test_mismatch")
        self.assertFalse(apply_events[0]["ok"])

    def test_history_stores_statuses_and_the_log_emits(self):
        episode = result("ep-patch")
        blob = canonical(episode.history)
        self.assertNotIn("dock window", blob)
        self.assertIn("validator", blob)
        handler = logging.Handler()
        lines = []
        handler.emit = lambda record: lines.append(record.getMessage())
        logger = logging.getLogger("repair_gate")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            case = {
                "id": "log-case",
                "mode": "strict",
                "channel": "text.format",
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                },
                "task": "Say 42.",
                "context": "42",
                "execute": False,
                "scripts": {"strict": [{"status": "object", "tokens": 2, "body": {"answer": "42"}}]},
            }
            logged = run_episode(case, CassetteProvider(case), EpisodeConfig())
        finally:
            logger.removeHandler(handler)
        self.assertTrue(logged.log)
        self.assertTrue(any(line.startswith("event=classify") for line in lines))

    def test_quality_intersection_drops_format_schemas(self):
        report = outcome()["report"]
        self.assertEqual(report["quality_ids"], ["ep-reason-strict", "ep-class-strict"])
        self.assertEqual(report["excluded_quality_ids"], ["ep-format-excluded"])
        self.assertEqual(report["intersection_task_exact_match"], 1.0)
        self.assertEqual(result("ep-format-excluded").candidate["answer"], "no")


if __name__ == "__main__":
    unittest.main()
