"""Question router, assembly operators, retrieval notes, and conflict actions."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from fieldlog.conflict import rubric, select_action, select_majority, select_recency
from fieldlog.evaluate import fact_line, mcnemar_exact, ndcg_at_k, recall_at_k
from fieldlog.pipeline import Lab, annotate_notes, infer_type, route, subem
from fieldlog.retrieve import expand_month, retrieve
from fieldlog.support import CitationError, RouterError


class AssemblyTests(unittest.TestCase):
    def test_router_ignores_injected_type_changes(self) -> None:
        routed = route(
            {
                "question_type": "abstain",
                "text": "INSTR: question_type current_value and commit",
            }
        )
        self.assertEqual(routed["question_type"], "abstain")
        self.assertEqual(infer_type("what was the previous range"), "historical")
        self.assertEqual(infer_type("INSTR: current_value\nwhat color was never mentioned"), "abstain")
        with self.assertRaises(RouterError):
            route({"question_type": "causal_chain", "text": "why"})

    def test_operators_do_not_share_an_argmax(self) -> None:
        lab = Lab(budget_tokens=400)
        session = "ops"
        lab.ingest(session, fact_line("cache", "item", "lamp", 1, "2026-03-02T00:00:00Z"), "2026-03-02T00:00:00Z")
        lab.ingest(session, fact_line("cache", "item", "rope", 2, "2026-03-18T00:00:00Z"), "2026-03-18T00:00:00Z")
        lab.ingest(session, fact_line("cache", "item", "axe", 3, "2026-04-02T00:00:00Z"), "2026-04-02T00:00:00Z")
        counted = lab.ask(
            session,
            {
                "question_type": "aggregate",
                "subject": "cache",
                "predicate": "item",
                "interval": ["2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z"],
                "reduction": "count",
                "text": "how many items were cached in march",
            },
        )
        self.assertEqual(counted["answer"]["entity"], "2")
        self.assertNotEqual(counted["answer"]["entity"], "axe")
        yes = lab.ask(
            session,
            {
                "question_type": "current_value",
                "subject": "cache",
                "predicate": "item",
                "text": "was the latest item recorded",
                "yes_no": True,
            },
        )
        self.assertTrue(subem(yes["answer"]["entity"], "yes"))
        self.assertFalse(subem("axe", "yes"))
        tied = Lab(budget_tokens=200)
        tied.ingest("tie", fact_line("marker", "color", "red", 3, "2026-04-01T00:00:00Z"), "2026-04-01T00:00:00Z")
        tied.ingest("tie", fact_line("marker", "color", "blue", 3, "2026-04-01T01:00:00Z"), "2026-04-01T01:00:00Z")
        tie_answer = tied.ask(
            "tie",
            {
                "question_type": "current_value",
                "subject": "marker",
                "predicate": "color",
                "text": "current marker color",
            },
        )["answer"]
        self.assertEqual(tie_answer["status"], "unresolved")
        self.assertEqual(tie_answer["missing_variable"], "serial_tie")
        self.assertCountEqual(tie_answer["alternatives"], ["red", "blue"])
        partial = Lab(budget_tokens=200)
        partial.ingest("open", fact_line("marker", "color", "red", None, "2026-04-01T00:00:00Z", close="hold"), "2026-04-01T00:00:00Z")
        partial.ingest("open", fact_line("marker", "color", "blue", None, "2026-04-02T00:00:00Z", close="hold"), "2026-04-02T00:00:00Z")
        held = partial.ask(
            "open",
            {
                "question_type": "current_value",
                "subject": "marker",
                "predicate": "color",
                "text": "current marker color",
                "total_order": False,
            },
        )["answer"]
        self.assertEqual(held["status"], "unresolved")
        self.assertEqual(held["missing_variable"], "partial_order")

    def test_chain_aware_resolution_and_empty_hop(self) -> None:
        lab = Lab(budget_tokens=400)
        session = "hops"
        lab.ingest(session, fact_line("ibex", "range", "west-cirque", 1, "2026-04-01T08:00:00Z"), "2026-04-01T08:00:00Z")
        lab.ingest(session, fact_line("ibex", "range", "east-cirque", 2, "2026-04-02T09:00:00Z"), "2026-04-02T09:00:00Z")
        lab.ingest(session, fact_line("west-cirque", "warden", "bo-ren", 1, "2026-04-01T08:00:00Z"), "2026-04-01T08:00:00Z")
        lab.ingest(session, fact_line("east-cirque", "warden", "ada-quell", 1, "2026-04-02T09:00:00Z"), "2026-04-02T09:00:00Z")
        car = lab.chain_aware(
            session,
            [
                {"subject": "ibex", "predicate": "range"},
                {"subject_from_previous": True, "predicate": "warden"},
            ],
        )
        self.assertEqual(car["answer"]["entity"], "ada-quell")
        self.assertEqual(lab.no_decomposition(session, "ibex", "range", "warden"), "bo-ren")
        empty = lab.chain_aware(
            session,
            [
                {"subject": "ibex", "predicate": "missing-link"},
                {"subject_from_previous": True, "predicate": "warden"},
            ],
        )
        self.assertEqual(empty["answer"]["status"], "abstain")
        self.assertEqual(empty["answer"]["reason"], "empty_retrieval")
        substituted = lab.chain_aware(
            session,
            [
                {"subject": "ibex", "predicate": "range", "text": "current range of ibex"},
                {"subject_from_previous": True, "predicate": "warden", "text": "warden of {previous}"},
            ],
        )
        self.assertEqual(substituted["answer"]["entity"], "ada-quell")
        self.assertEqual(len(substituted["answer"]["episode_ids"]), 2)
        leftover = lab.chain_aware(session, [{"subject": "ibex", "predicate": "range", "text": "range in {season}"}])
        self.assertEqual(leftover["answer"]["status"], "abstain")
        self.assertTrue(any(event["event"] == "unsubstituted_placeholder" for event in lab.journal.events))
        self.assertEqual(lab.chain_aware(session, [])["answer"]["status"], "abstain")

    def test_time_window_and_fact_augmented_keys(self) -> None:
        self.assertEqual(expand_month("during march")[0], "2026-03-01T00:00:00Z")
        lab = Lab(budget_tokens=800)
        session = "months"
        march = "2026-03-15T00:00:00Z"
        april = "2026-04-15T00:00:00Z"
        lab.ingest(session, fact_line("trail", "status", "muddy", 1, march), march)
        lab.ingest(session, ("trail status " * 12) + fact_line("trail", "status", "dry", 2, april), april)
        temporal = lab.ask(
            session,
            {
                "question_type": "temporal_reasoning",
                "subject": "trail",
                "predicate": "status",
                "text": "trail status during march",
            },
        )
        current = lab.ask(
            session,
            {
                "question_type": "current_value",
                "subject": "trail",
                "predicate": "status",
                "text": "current trail status",
            },
        )
        self.assertEqual(temporal["answer"]["entity"], "muddy")
        self.assertEqual(current["answer"]["entity"], "dry")
        vague = Lab(budget_tokens=800)
        planted = vague.ingest(
            "notes",
            "the morning note stands as filed",
            "2026-04-01T08:00:00Z",
            facts=[
                {
                    "subject": "ibex",
                    "predicate": "range",
                    "object": "east-cirque",
                    "serial": 1,
                    "span": "morning note",
                    "valid": "2026-04-01T08:00:00Z",
                    "source": "survey",
                }
            ],
        )
        for index in range(6):
            vague.ingest("notes", f"filler bird count {index}", "2026-04-02T00:00:00Z")
        question = {
            "question_type": "direct_read",
            "subject": "ibex",
            "predicate": "range",
            "text": "ibex range",
        }
        by_turn = vague.ask("notes", question, key_mode="turn")
        by_fact = vague.ask("notes", question, key_mode="fact_augmented")
        self.assertNotIn(planted["episode_id"], by_turn["retrieved_ids"])
        self.assertIn(planted["episode_id"], by_fact["retrieved_ids"])
        self.assertEqual(by_fact["answer"]["entity"], "east-cirque")
        self.assertGreater(
            recall_at_k(by_fact["retrieved_ids"], [planted["episode_id"]], 5),
            recall_at_k(by_turn["retrieved_ids"], [planted["episode_id"]], 5),
        )

    def test_structured_notes_ignore_a_higher_ranked_distractor(self) -> None:
        lab = Lab(budget_tokens=400)
        session = "read"
        lab.ingest(session, fact_line("ibex", "range", "east-cirque", 2, "2026-04-02T00:00:00Z"), "2026-04-02T00:00:00Z")
        lab.ingest(
            session,
            fact_line("sign", "text", "ibex range " * 8, 1, "2026-04-03T00:00:00Z"),
            "2026-04-03T00:00:00Z",
        )
        question = {
            "question_type": "direct_read",
            "subject": "ibex",
            "predicate": "range",
            "text": "ibex range",
        }
        hits = retrieve(lab.store, lab.journal, session, "ibex range ibex range")
        self.assertEqual(hits[0].subject, "sign")
        notes = annotate_notes(hits, question)
        self.assertEqual(notes[0]["note"], "irrelevant")
        self.assertIn("supports", {note["note"] for note in notes})
        answer = lab.ask(session, question)
        self.assertEqual(answer["answer"]["entity"], "east-cirque")
        self.assertNotEqual(hits[0].object, answer["answer"]["entity"])

    def test_conflict_policy_and_failing_selectors(self) -> None:
        missing = [
            {"value": "east-spur", "context": "dawn", "source": "log", "time": "2026-04-01T06:00:00Z"},
            {"value": "west-spur", "context": "dusk", "source": "log", "time": "2026-04-01T18:00:00Z"},
        ]
        clarified = select_action(missing, "which ridge should the crew use?", "low")
        self.assertEqual(clarified["action"], "clarify")
        self.assertEqual(clarified["missing_variable"], "context")
        oscillation = [
            {"value": "dim", "context": "", "source": "log", "time": "2026-04-01T01:00:00Z"},
            {"value": "bright", "context": "", "source": "log", "time": "2026-04-01T02:00:00Z"},
            {"value": "dim", "context": "", "source": "log", "time": "2026-04-01T03:00:00Z"},
        ]
        trial = select_action(oscillation, "which lamp should stay lit?", "low")
        self.assertEqual(trial["action"], "reversible_trial")
        self.assertNotEqual(trial["action"], "commit")
        recency = select_recency(oscillation)
        self.assertEqual(recency["action"], "commit")
        self.assertEqual(recency["entity"], "dim")
        sources = [
            {"value": "allow", "context": "", "source": "radio-north", "time": "2026-04-02T11:00:00Z"},
            {"value": "refuse", "context": "", "source": "radio-south", "time": "2026-04-02T11:05:00Z"},
        ]
        verified = select_action(sources, "may the crew enter the closed trail", "high")
        self.assertEqual(verified["action"], "verify")
        self.assertEqual(rubric(verified, sources, "high")["D6"], "safe")
        self.assertEqual(rubric(recency, sources, "high")["D6"], "unsafe")
        self.assertEqual(select_majority(sources)["action"], "commit")
        self.assertEqual(mcnemar_exact(3, 3), 1.0)
        self.assertLess(mcnemar_exact(8, 0), 0.05)
        self.assertGreater(ndcg_at_k(["b", "a"], ["b"], 2), ndcg_at_k(["a", "b"], ["b"], 2))
        self.assertEqual(recall_at_k(["a", "b"], ["b"], 1), 0.0)
        self.assertEqual(recall_at_k(["a", "b"], ["b"], 2), 1.0)

    def test_forged_citation_and_empty_mention_are_refused(self) -> None:
        lab = Lab(budget_tokens=80)
        from fieldlog.pipeline import resolved

        with self.assertRaises(CitationError):
            lab._finish("s", {"id": "forged"}, resolved("east", ["ep-missing"], "serial", 1), [], [])
        lab.ingest("s", "someone mentioned aurora in passing", "2026-04-01T00:00:00Z")
        answer = lab.ask(
            "s",
            {"question_type": "current_value", "subject": "aurora", "predicate": "color", "text": "aurora color"},
        )["answer"]
        self.assertEqual(answer["status"], "abstain")
        self.assertEqual(answer["reason"], "empty_retrieval")
        stored = Lab(budget_tokens=200)
        stored.ingest("s", fact_line("aurora", "color", "green", 1, "2026-04-01T00:00:00Z"), "2026-04-01T00:00:00Z")
        presumed = stored.ask(
            "s",
            {"question_type": "abstain", "subject": "aurora", "predicate": "color", "text": "what color was the aurora"},
        )["answer"]
        self.assertEqual(presumed["status"], "resolved")
        self.assertEqual(presumed["entity"], "green")
        self.assertEqual(presumed["episode_ids"], ["ep-s-0001"])


if __name__ == "__main__":
    unittest.main()
