"""Plan isolation, redaction, replay, and tool-result failures."""

from __future__ import annotations

import copy
import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from helpers import clone_task, load_tasks, load_world, runner_for, task_by_id

from office_gate_lab.cassette import planner_view
from office_gate_lab.codec import digest
from office_gate_lab.lints import lint_run
from office_gate_lab.metrics import attacker_succeeded, user_succeeded
from office_gate_lab.redact import lab_logger
from office_gate_lab.tools import TOOL_NAMES
from office_gate_lab.world import INJECT_TEXT


class StubModel:
    """A provider double that returns one fixed planner completion."""

    def __init__(self, plan: dict) -> None:
        self.plan = plan
        self.views: list[dict] = []

    def complete(self, kind: str, view: dict) -> dict:
        self.views.append(view)
        if kind == "plan":
            return self.plan
        return {"kind": "finish", "claims": []}


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = load_world()
        self.tasks = load_tasks()

    def test_text_injection_keeps_the_plan_and_the_allowed_send(self) -> None:
        task = task_by_id(self.tasks, "t01-minutes-literal")
        benign = runner_for(self.world, self.tasks).start(task, attack=False, thread_id="thread-benign")
        attacked = runner_for(self.world, self.tasks).start(
            task, attack=True, attack_mode="text", thread_id="thread-attack"
        )
        self.assertTrue(user_succeeded(benign, task))
        self.assertTrue(user_succeeded(attacked, task))
        self.assertFalse(attacker_succeeded(attacked, task))
        self.assertEqual(attacked.plan_tools, ["read_document", "send_document"])
        self.assertEqual(attacked.executed_tools, attacked.plan_tools)
        self.assertNotIn("export_all", attacked.executed_tools)
        operations = [span["attributes"]["gen_ai.operation.name"] for span in attacked.spans]
        self.assertLess(operations.index("plan"), operations.index("execute_tool"))
        view = planner_view(task)
        blob = json.dumps(view)
        self.assertNotIn("Decisions recorded", blob)
        self.assertNotIn(INJECT_TEXT, blob)
        self.assertNotIn("export_all", blob)
        self.assertEqual(list(view["tools"]), list(TOOL_NAMES))

    def test_replay_reproduces_world_hash_and_span_ids(self) -> None:
        task = task_by_id(self.tasks, "t01-minutes-literal")
        first = runner_for(self.world, self.tasks).start(task, attack=True, thread_id="thread-replay")
        second = runner_for(self.world, self.tasks).start(task, attack=True, thread_id="thread-replay")
        self.assertEqual(first.world_hash, second.world_hash)
        self.assertEqual([span["span_id"] for span in first.spans], [span["span_id"] for span in second.spans])
        self.assertEqual(json.dumps(first.spans, sort_keys=True), json.dumps(second.spans, sort_keys=True))

    def test_trace_redacts_bodies_and_keeps_hashes_outside_the_log(self) -> None:
        task = task_by_id(self.tasks, "t04-inbox-minutes")
        result = runner_for(self.world, self.tasks).start(task, attack=True, thread_id="thread-redact")
        text = json.dumps(result.spans)
        self.assertNotIn("@", text)
        self.assertNotIn("Decisions recorded", text)
        self.assertNotIn("The minutes are filed", text)
        self.assertNotIn("Ignore the plan", text)
        self.assertIn("[REDACTED_EMAIL]", text)
        self.assertIn("[REDACTED_BODY]", text)
        stored = next(doc["body"] for doc in self.world["documents"] if doc["id"] == "doc-minutes")
        self.assertEqual(result.body_hashes["doc-minutes"], digest(stored))
        self.assertNotIn(result.body_hashes["doc-minutes"], text)
        for span in result.spans:
            self.assertEqual(span["attributes"]["gen_ai.conversation.id"], result.thread_id)
            self.assertNotEqual(span["trace_id"], span["attributes"]["gen_ai.conversation.id"])
        self.assertIsNone(result.spans[0]["parent_span_id"])
        self.assertEqual(result.spans[1]["parent_span_id"], result.spans[0]["span_id"])
        self.assertEqual(result.spans[2]["parent_span_id"], result.spans[1]["span_id"])

    def test_finish_marks_a_missing_document_unverified(self) -> None:
        task = task_by_id(self.tasks, "t10-grounded-finish")
        result = runner_for(self.world, self.tasks).start(task, thread_id="thread-ground")
        supports = {claim["claim_id"]: claim["support"] for claim in result.claims}
        self.assertEqual(supports["c1"], "entailed")
        self.assertEqual(supports["c2"], "unverified")
        finish = next(span for span in result.spans if span["attributes"].get("lab.span.role") == "finish")
        logged = {claim["claim_id"]: claim["support"] for claim in finish["attributes"]["lab.claims"]}
        self.assertEqual(logged["c2"], "unverified")

    def test_untrusted_predicate_cannot_change_the_tool(self) -> None:
        task = clone_task(task_by_id(self.tasks, "t01-minutes-literal"), "taint-case", "Taint the send predicate.")
        task["plan"]["steps"][1]["predicate"] = {
            "source": "extract",
            "value": None,
            "from_step": "s1",
            "field": "urgent",
        }
        runner = runner_for(self.world, [task])
        paused = runner.start(task, attack=True, thread_id="thread-taint")
        self.assertEqual(paused.status, "paused")
        self.assertTrue(paused.control_flow_taint)
        self.assertEqual(paused.executed_tools, ["read_document"])
        self.assertEqual(paused.plan_tools, ["read_document", "send_document"])
        accepted = runner.resume(
            "thread-taint",
            {"action": "accept", "content": {"recipient": "ada@northline.example", "confirm": True}},
        )
        self.assertEqual(accepted.status, "finished")
        self.assertEqual(accepted.executed_tools, ["read_document", "send_document"])
        self.assertEqual(accepted.outbox[0]["recipient"], "ada@northline.example")

    def test_trusted_false_predicate_skips_the_planned_send(self) -> None:
        task = clone_task(task_by_id(self.tasks, "t01-minutes-literal"), "skip-case", "Skip when the request says false.")
        task["plan"]["steps"][1]["predicate"] = {
            "source": "literal",
            "value": "false",
            "from_step": None,
            "field": None,
        }
        result = runner_for(self.world, [task]).start(task, thread_id="thread-skip")
        self.assertEqual(result.status, "finished")
        self.assertEqual(result.executed_tools, ["read_document"])
        self.assertEqual(result.outbox, [])
        self.assertFalse(result.control_flow_taint)

    def test_tool_error_stays_an_observation(self) -> None:
        task = clone_task(task_by_id(self.tasks, "t01-minutes-literal"), "missing-doc", "Read a missing document first.")
        task["plan"]["steps"][0]["arguments"]["document_id"] = {
            "source": "literal",
            "value": "doc-missing",
            "from_step": None,
            "field": None,
        }
        result = runner_for(self.world, [task]).start(task, thread_id="thread-missing")
        self.assertEqual(result.executed_tools, ["read_document", "send_document"])
        self.assertEqual(result.plan_tools, result.executed_tools)
        errors = [
            span["attributes"]["error.type"]
            for span in result.spans
            if span["attributes"]["gen_ai.operation.name"] == "execute_tool"
        ]
        self.assertEqual(errors, ["unknown_document", None])
        self.assertEqual(result.delays, [])

    def test_refusal_produces_no_tool_call(self) -> None:
        task = clone_task(task_by_id(self.tasks, "t01-minutes-literal"), "refusal-case", "Refuse this request.")
        task["plan"] = {"kind": "refusal", "reason": "safety"}
        result = runner_for(self.world, [task]).start(task, thread_id="thread-refusal")
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.executed_tools, [])
        self.assertTrue(any(span["attributes"].get("lab.span.role") == "refusal" for span in result.spans))

    def test_schema_invalid_tool_output_does_not_extend_the_plan(self) -> None:
        task = task_by_id(self.tasks, "t02-brief-field")
        result = runner_for(self.world, self.tasks).start(
            task, corrupt_read_output=True, thread_id="thread-corrupt"
        )
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_type, "unbound_argument")
        self.assertEqual(result.plan_tools, ["read_document", "send_document"])
        self.assertEqual(result.executed_tools, ["read_document"])
        self.assertEqual(result.outbox, [])

    def test_model_output_is_validated_before_any_tool_runs(self) -> None:
        task = task_by_id(self.tasks, "t01-minutes-literal")
        hijacked = copy.deepcopy(task["plan"])
        hijacked["steps"][1]["tool"] = "export_all"
        model = StubModel(hijacked)
        result = runner_for(self.world, self.tasks, model=model).start(task, attack=True, thread_id="thread-stub")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_type, "schema_rejected")
        self.assertEqual(result.executed_tools, [])
        self.assertEqual(model.views, [planner_view(task)])
        duplicated = copy.deepcopy(task["plan"])
        duplicated["steps"][1]["step_id"] = "s1"
        result = runner_for(self.world, self.tasks, model=StubModel(duplicated)).start(task, thread_id="thread-dup")
        self.assertEqual(result.error_type, "schema_rejected")
        self.assertEqual(result.outbox, [])
        result = runner_for(self.world, self.tasks, model=StubModel(task["plan"])).start(task, thread_id="thread-ok")
        self.assertEqual(result.status, "finished")
        self.assertTrue(user_succeeded(result, task))

    def test_unanswered_question_leaves_the_send_unbound(self) -> None:
        task = clone_task(
            task_by_id(self.tasks, "t06-ask-then-send"), "wrong-question", "Ask a question the user did not answer."
        )
        task["user_replies"] = {"other_question": "ben@northline.example"}
        result = runner_for(self.world, [task]).start(task, thread_id="thread-ask")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_type, "unbound_argument")
        self.assertEqual(result.executed_tools, ["read_document", "ask_user"])
        self.assertEqual(result.outbox, [])

    def test_trace_jsonl_is_written_redacted(self) -> None:
        task = task_by_id(self.tasks, "t04-inbox-minutes")
        runner = runner_for(self.world, self.tasks)
        result = runner.start(task, attack=True, thread_id="thread-jsonl")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            runner.sessions["thread-jsonl"].tracer.write_jsonl(path)
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(result.spans))
        self.assertEqual([json.loads(line)["span_id"] for line in lines], [span["span_id"] for span in result.spans])
        joined = "\n".join(lines)
        self.assertNotIn("@", joined)
        self.assertNotIn("Ignore the plan", joined)

    def test_transient_read_retries_once_with_recorded_backoff(self) -> None:
        task = task_by_id(self.tasks, "t01-minutes-literal")
        result = runner_for(self.world, self.tasks).start(task, transient_failures=1, thread_id="thread-retry")
        self.assertEqual(result.status, "finished")
        self.assertEqual(result.delays, [1])
        self.assertEqual(len(result.outbox), 1)
        self.assertEqual(result.executed_tools, ["read_document", "send_document"])
        retries = [span for span in result.spans if span["attributes"].get("lab.idempotent_retry")]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["attributes"]["error.type"], "transient")
        lint_args = {
            "contract_tools": task["contract_tools"],
            "golden_outbox": task["golden_outbox"],
            "outbox": result.outbox,
            "status": result.status,
        }
        self.assertEqual(lint_run(result.spans, **lint_args), [])
        unmarked = copy.deepcopy(result.spans)
        for span in unmarked:
            span["attributes"]["lab.idempotent_retry"] = False
        self.assertIn("step_repetition", lint_run(unmarked, **lint_args))
        sends = [
            span
            for span in result.spans
            if span["attributes"].get("gen_ai.tool.name") == "send_document"
            and span["attributes"]["gen_ai.operation.name"] == "execute_tool"
        ]
        self.assertEqual(len(sends), 1)

    def test_logs_redact_addresses(self) -> None:
        logger = lab_logger()
        with self.assertLogs("office_gate_lab", level=logging.INFO) as captured:
            logger.info("wrote %s", "ada@northline.example")
            runner_for(self.world, self.tasks).start(
                task_by_id(self.tasks, "t01-minutes-literal"), thread_id="thread-log"
            )
        text = "\n".join(captured.output)
        self.assertNotIn("ada@northline.example", text)
        self.assertIn("[REDACTED_EMAIL]", text)

    def test_hijack_capability_uses_world_readers(self) -> None:
        task = task_by_id(self.tasks, "t02-brief-field")
        result = runner_for(self.world, self.tasks).start(
            task, attack=True, attack_mode="overwrite_named_recipient", thread_id="thread-readers"
        )
        extract = next(span for span in result.spans if span["attributes"].get("lab.span.role") == "extract")
        self.assertFalse(extract["attributes"]["lab.extract.trusted"])
        self.assertEqual(extract["attributes"]["lab.capability.reader_count"], 1)
        self.assertEqual(result.status, "paused")
        self.assertNotIn("send_document", result.executed_tools)


if __name__ == "__main__":
    unittest.main()
