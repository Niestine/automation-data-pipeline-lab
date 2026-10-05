"""Checkpoint resume, elicitation rejection, and exactly-once sends."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from helpers import clone_task, load_tasks, load_world, runner_for, task_by_id

from office_gate_lab.errors import LabError
from office_gate_lab.lints import approval_coverage
from office_gate_lab.runner import OfficeRunner
from office_gate_lab.world import ATTACKER

BEN = "ben@northline.example"
ACCEPT_BEN = {"action": "accept", "content": {"recipient": BEN, "confirm": True}}


def _all_send_spans(result) -> list:
    return [
        span
        for span in result.spans
        if span["attributes"]["gen_ai.operation.name"] == "execute_tool"
        and span["attributes"].get("gen_ai.tool.name") == "send_document"
    ]


def _send_spans(result) -> list:
    """Send executions that wrote, excluding ledger replays."""
    return [span for span in _all_send_spans(result) if not span["attributes"]["lab.ledger.replay"]]


def _replay_spans(result) -> list:
    return [span for span in _all_send_spans(result) if span["attributes"]["lab.ledger.replay"]]


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = load_world()
        self.tasks = load_tasks()
        self.task = task_by_id(self.tasks, "t02-brief-field")

    def _paused(self, directory: str, **kwargs) -> tuple[OfficeRunner, Path, object]:
        path = Path(directory) / "checkpoint.jsonl"
        runner = runner_for(self.world, self.tasks, checkpoint_path=path, **kwargs)
        result = runner.start(
            self.task,
            attack=True,
            attack_mode="overwrite_named_recipient",
            thread_id="thread-gate",
        )
        return runner, path, result

    def test_invalid_resume_keeps_the_checkpoint_and_blocks_the_send(self) -> None:
        with TemporaryDirectory() as directory:
            runner, path, paused = self._paused(directory)
            self.assertEqual(paused.status, "paused")
            self.assertEqual(paused.outbox, [])
            self.assertEqual(_send_spans(paused), [])
            raw = path.read_bytes()
            missing = runner.resume("thread-gate", {})
            self.assertEqual(missing.status, "rejected")
            self.assertEqual(missing.error_type, "missing_action")
            self.assertEqual(path.read_bytes(), raw)
            declined = runner.resume(
                "thread-gate",
                {"action": "decline", "content": {"recipient": BEN, "confirm": True}},
            )
            self.assertEqual(declined.status, "rejected")
            self.assertEqual(declined.error_type, "content_on_decline")
            self.assertEqual(path.read_bytes(), raw)
            bare = runner.resume("thread-gate", {"action": "accept"})
            self.assertEqual(bare.status, "rejected")
            self.assertEqual(bare.error_type, "accept_without_content")
            self.assertEqual(path.read_bytes(), raw)
            unticked = runner.resume(
                "thread-gate",
                {"action": "accept", "content": {"recipient": BEN, "confirm": False}},
            )
            self.assertEqual(unticked.status, "rejected")
            self.assertEqual(unticked.error_type, "accept_not_confirmed")
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(runner.sessions["thread-gate"].world.outbox, [])

    def test_edited_non_reader_does_not_send_and_an_allowed_edit_sends_once(self) -> None:
        with TemporaryDirectory() as directory:
            runner, path, _paused = self._paused(directory)
            raw = path.read_bytes()
            blocked = runner.resume(
                "thread-gate",
                {"action": "accept", "content": {"recipient": ATTACKER, "confirm": True}},
            )
            self.assertEqual(blocked.status, "paused")
            self.assertEqual(blocked.outbox, [])
            self.assertEqual(_send_spans(blocked), [])
            self.assertEqual(path.read_bytes(), raw)
            accepted = runner.resume(
                "thread-gate",
                {"action": "accept", "content": {"recipient": BEN, "confirm": True}},
            )
            self.assertEqual(accepted.status, "finished")
            self.assertEqual(len(accepted.outbox), 1)
            self.assertEqual(accepted.outbox[0]["recipient"], BEN)
            self.assertEqual(len(_send_spans(accepted)), 1)
            self.assertTrue(approval_coverage(accepted.spans))
            self.assertEqual(_replay_spans(accepted), [])
            moved = copy.deepcopy(accepted.spans)
            for span in moved:
                if span["attributes"]["gen_ai.operation.name"] == "execute_tool" and span["attributes"]["lab.tool.mutates"]:
                    span["attributes"]["gen_ai.tool.call.id"] = "call-thread-gate-other"
            self.assertFalse(approval_coverage(moved))
            again = runner.resume("thread-gate", ACCEPT_BEN)
            self.assertEqual(again.status, "finished")
            self.assertEqual(len(again.outbox), 1)
            self.assertEqual(len(_send_spans(again)), 1)
            replay = _replay_spans(again)
            self.assertEqual(len(replay), 1)
            self.assertEqual(
                replay[0]["attributes"]["gen_ai.tool.call.id"],
                _send_spans(again)[0]["attributes"]["gen_ai.tool.call.id"],
            )
            self.assertEqual(again.executed_tools, accepted.executed_tools)
            self.assertEqual(again.world_hash, accepted.world_hash)
            self.assertTrue(approval_coverage(again.spans))
            self.assertEqual(path.read_bytes(), raw)

    def test_resume_reads_the_persisted_checkpoint(self) -> None:
        with TemporaryDirectory() as directory:
            runner, path, _paused = self._paused(directory)
            record = runner.store.latest("thread-gate")
            self.assertEqual(record["call_id"], "call-thread-gate-s2")
            self.assertEqual(record["step_index"], 1)
            path.write_text("", encoding="utf-8")
            result = runner.resume("thread-gate", ACCEPT_BEN)
            self.assertEqual(result.status, "rejected")
            self.assertEqual(result.error_type, "no_checkpoint")
            self.assertEqual(result.outbox, [])

    def test_cancelled_or_declined_threads_cannot_be_revived(self) -> None:
        runner = runner_for(self.world, self.tasks)
        runner.start(self.task, attack=True, attack_mode="overwrite_named_recipient", thread_id="thread-c")
        self.assertEqual(runner.resume("thread-c", {"action": "cancel"}).status, "cancelled")
        revived = runner.resume("thread-c", ACCEPT_BEN)
        self.assertEqual(revived.status, "rejected")
        self.assertEqual(revived.error_type, "nothing_to_resume")
        self.assertEqual(revived.outbox, [])
        self.assertEqual(_all_send_spans(revived), [])
        runner.start(self.task, attack=True, attack_mode="overwrite_named_recipient", thread_id="thread-d")
        self.assertEqual(runner.resume("thread-d", {"action": "decline"}).status, "declined")
        revived = runner.resume("thread-d", ACCEPT_BEN)
        self.assertEqual(revived.error_type, "nothing_to_resume")
        self.assertEqual(revived.outbox, [])
        with self.assertRaises(LabError):
            runner.resume("thread-unknown", ACCEPT_BEN)

    def test_each_paused_call_gets_its_own_checkpoint_record(self) -> None:
        task = clone_task(self.task, "two-pauses", "Send the brief twice to the extracted recipient.")
        second_send = copy.deepcopy(task["plan"]["steps"][1])
        second_send["step_id"] = "s3"
        task["plan"]["steps"].append(second_send)
        task["contract_tools"] = ["read_document", "send_document", "send_document"]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.jsonl"
            runner = runner_for(self.world, [task], checkpoint_path=path)
            first = runner.start(task, attack=True, attack_mode="overwrite_named_recipient", thread_id="thread-2p")
            self.assertEqual(first.status, "paused")
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)
            second = runner.resume("thread-2p", ACCEPT_BEN)
            self.assertEqual(second.status, "paused")
            self.assertEqual(len(second.outbox), 1)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(runner.store.latest("thread-2p")["call_id"], "call-thread-2p-s3")
            done = runner.resume("thread-2p", ACCEPT_BEN)
            self.assertEqual(done.status, "finished")
            self.assertEqual([row["tool_call_id"] for row in done.outbox], ["call-thread-2p-s2", "call-thread-2p-s3"])

    def test_structural_pass_with_format_assertion_off_still_needs_the_allowlist(self) -> None:
        with TemporaryDirectory() as directory:
            runner, _path, paused = self._paused(directory, format_assertion=False)
            self.assertEqual(paused.status, "paused")
            edited = runner.resume(
                "thread-gate",
                {"action": "accept", "content": {"recipient": "not-an-email", "confirm": True}},
            )
            self.assertEqual(edited.status, "paused")
            self.assertEqual(edited.outbox, [])
            self.assertEqual(_send_spans(edited), [])

    def test_secret_form_is_rejected_before_a_checkpoint(self) -> None:
        schema = {
            "type": "object",
            "properties": {"token": {"type": "string", "description": "A label"}},
            "required": ["token"],
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.jsonl"
            runner = runner_for(self.world, self.tasks, checkpoint_path=path, elicitation_schema=schema)
            result = runner.start(
                self.task,
                attack=True,
                attack_mode="overwrite_named_recipient",
                thread_id="thread-secret",
            )
            self.assertEqual(result.status, "error")
            self.assertEqual(result.error_type, "elicitation_secret")
            self.assertFalse(path.exists())
            self.assertEqual(result.outbox, [])

    def test_read_only_hint_still_pauses_a_real_send(self) -> None:
        runner = runner_for(
            self.world,
            self.tasks,
            external_annotations={"send_document": {"annotations": {"readOnlyHint": True}}},
        )
        result = runner.start(
            self.task,
            attack=True,
            attack_mode="overwrite_named_recipient",
            thread_id="thread-hint",
        )
        self.assertEqual(result.status, "paused")
        self.assertNotIn("send_document", result.executed_tools)

    def test_decline_continues_only_into_a_later_read(self) -> None:
        task = clone_task(self.task, "decline-read", "Decline the send and then read the roster.")
        task["plan"]["steps"].append(
            {
                "step_id": "s3",
                "tool": "read_document",
                "arguments": {
                    "document_id": {
                        "source": "literal",
                        "value": "doc-roster",
                        "from_step": None,
                        "field": None,
                    },
                    "recipient": None,
                    "question_id": None,
                },
                "predicate": None,
            }
        )
        runner = runner_for(self.world, [task])
        paused = runner.start(task, attack=True, attack_mode="overwrite_named_recipient", thread_id="thread-dec")
        self.assertEqual(paused.executed_tools, ["read_document"])
        continued = runner.resume("thread-dec", {"action": "decline"})
        self.assertEqual(continued.status, "finished")
        self.assertEqual(continued.executed_tools, ["read_document", "read_document"])
        self.assertEqual(continued.outbox, [])

        stopped = clone_task(self.task, "decline-stop", "Decline and do not continue into another send.")
        stopped["plan"]["steps"].append(
            {
                "step_id": "s3",
                "tool": "send_document",
                "arguments": {
                    "document_id": {
                        "source": "literal",
                        "value": "doc-brief",
                        "from_step": None,
                        "field": None,
                    },
                    "recipient": {
                        "source": "literal",
                        "value": BEN,
                        "from_step": None,
                        "field": None,
                    },
                    "question_id": None,
                },
                "predicate": None,
            }
        )
        runner = runner_for(self.world, [stopped])
        runner.start(stopped, attack=True, attack_mode="overwrite_named_recipient", thread_id="thread-stop")
        result = runner.resume("thread-stop", {"action": "decline"})
        self.assertEqual(result.status, "declined")
        self.assertEqual(result.executed_tools, ["read_document"])
        self.assertEqual(result.outbox, [])

    def test_cancel_sets_approval_cancelled(self) -> None:
        runner = runner_for(self.world, self.tasks)
        runner.start(
            self.task,
            attack=True,
            attack_mode="overwrite_named_recipient",
            thread_id="thread-cancel",
        )
        result = runner.resume("thread-cancel", {"action": "cancel"})
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.error_type, "approval_cancelled")
        workflow = result.spans[0]["attributes"]["error.type"]
        self.assertEqual(workflow, "approval_cancelled")
        self.assertEqual(result.outbox, [])

    def test_preapproved_send_records_a_policy_span(self) -> None:
        task = task_by_id(self.tasks, "t01-minutes-literal")
        result = runner_for(self.world, self.tasks).start(task, thread_id="thread-pre")
        self.assertTrue(approval_coverage(result.spans))
        policy = [
            span
            for span in result.spans
            if span["attributes"]["gen_ai.operation.name"] == "policy_check"
        ]
        self.assertEqual(len(policy), 1)
        self.assertTrue(policy[0]["attributes"]["lab.policy.preapproved"])
        self.assertFalse(
            any(span["attributes"]["gen_ai.operation.name"] == "request_approval" for span in result.spans)
        )


if __name__ == "__main__":
    unittest.main()
