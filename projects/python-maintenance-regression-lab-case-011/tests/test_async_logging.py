"""Await points, generator lifetime, and the logging lock repair."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from support import PROJECT, REGRESSIONS, execute, scenario
from yieldlab.agen import AsyncGen, LoopHooks, cursor_rows
from yieldlab.engine import Schedule, search_first
from yieldlab.errors import (
    GeneratorReentered,
    Invariant,
    NeverRetrieved,
    ReplayDivergence,
    WrongThread,
)
from yieldlab.journal import dump_journal, load_journal
from yieldlab.loggingx import Handler, Manager
from yieldlab.scenarios import build, load_levels, load_port


class AsyncLoggingTests(unittest.TestCase):
    def test_partner_waits_for_await(self) -> None:
        case = scenario(
            "coop",
            {
                1: [("bc", "a1"), ("bc", "a2"), ("await",), ("bc", "a3")],
                2: [("bc", "partner")],
            },
            kinds={1: "loop", 2: "loop"},
            loop_tid=1,
        )
        trace = execute(case, policy="guide", guide=[1, 1, 1, 2, 1])
        ops = [(event["thread"], event["op"]) for event in trace.events]
        self.assertLess(ops.index((1, "await")), ops.index((2, "bc")))
        with self.assertRaises(ReplayDivergence):
            execute(case, policy="guide", guide=[1, 2])

    def test_executor_lets_the_partner_run(self) -> None:
        blocked = scenario(
            "cpu",
            {1: [("bc", "cpu"), ("bc", "cpu"), ("bc", "cpu")], 2: [("bc", "partner")]},
            kinds={1: "loop", 2: "loop"},
        )
        trace = execute(blocked, policy="guide", guide=[1, 1, 1, 2])
        self.assertEqual(trace.events[-1]["thread"], 2)
        with self.assertRaises(ReplayDivergence):
            execute(blocked, policy="guide", guide=[1, 2])

        offloaded = scenario(
            "executor",
            {1: [("allow",), ("bc", "after")], 2: [("bc", "partner")]},
            kinds={1: "loop", 2: "loop"},
        )
        trace = execute(offloaded, policy="guide", guide=[1, 2, 1])
        marks = [event["args"][0] for event in trace.events if event["args"]]
        self.assertLess(marks.index("partner"), marks.index("after"))

    def test_debug_call_soon_and_threadsafe(self) -> None:
        wrong = scenario(
            "soon",
            {1: [("bc", "loop")], 2: [("call_soon",)]},
            kinds={1: "loop", 2: "thread"},
            debug=True,
            loop_tid=1,
        )
        with self.assertRaises(WrongThread):
            execute(wrong, policy="guide", guide=[2])
        safe = scenario(
            "soon_ts",
            {2: [("call_soon_ts", "ping")], 1: [("pump",)]},
            kinds={1: "loop", 2: "thread"},
            debug=True,
            loop_tid=1,
        )
        trace = execute(safe, policy="guide", guide=[2, 1])
        self.assertEqual(trace.extra["ran"], [(1, "ping")])

    def test_coroutine_future(self) -> None:
        case = scenario(
            "future",
            {2: [("submit",)], 1: [("coro",)]},
            kinds={1: "loop", 2: "thread"},
            loop_tid=1,
        )
        trace = execute(case, policy="guide", guide=[2, 1])
        self.assertEqual(trace.extra["future"]["result"], 7)

    def test_never_retrieved_and_await(self) -> None:
        lost = scenario(
            "lost_task",
            {1: [("arm_task",), ("boom",)]},
            debug=True,
            kinds={1: "loop"},
        )
        trace = execute(lost)
        self.assertEqual(len(trace.logs), 1)
        self.assertIn("Task exception was never retrieved", trace.logs[0])
        self.assertIn("create_task at scenario:never_retrieved", trace.logs[0])
        with self.assertRaises(NeverRetrieved):
            execute(lost, strict_tasks=True)

        kept = scenario(
            "kept_task",
            {1: [("arm_task",), ("boom",), ("await_task",)]},
            debug=True,
            kinds={1: "loop"},
        )
        trace = execute(kept)
        self.assertEqual(trace.logs, [])
        self.assertEqual(trace.extra["surfaced"], "boom")

    def test_slow_callback_threshold(self) -> None:
        case = scenario(
            "slow",
            {1: [("bc",), ("bc",), ("bc",), ("bc",), ("bc",), ("await",), ("bc",), ("bc",)]},
            kinds={1: "loop"},
            slow_callback_steps=3,
        )
        trace = execute(case)
        # Five steps cross the cutoff once; the await starts a new callback
        # that stays under it.
        self.assertEqual(trace.logs, ["slow callback"])

    def test_cursor_rows_and_reentrancy_and_early_gen(self) -> None:
        with self.assertRaises(Invariant):
            cursor_rows(False)
        self.assertEqual(cursor_rows(True), "ok")

        loop = LoopHooks()
        loop.start()
        gen = AsyncGen(loop, "g")
        gen.prime()
        gen.asend()
        with self.assertRaises(GeneratorReentered) as caught:
            gen.asend()
        self.assertIn("already running", str(caught.exception))
        with self.assertRaises(GeneratorReentered):
            gen.aclose()
        # Once the first asend reaches its yield, the next one is legal.
        gen.resume_done()
        self.assertEqual(gen.asend(), "yield")

        early = LoopHooks()
        gen = AsyncGen(early, "early")
        gen.prime()
        early.start()
        early.shutdown_asyncgens()
        with self.assertRaises(RuntimeError) as runtime_error:
            gen.finalize()
        self.assertIn("GeneratorExit", str(runtime_error.exception))

        hosted = LoopHooks()
        hosted.start()
        gen = AsyncGen(hosted, "hosted")
        gen.prime()
        hosted.shutdown_asyncgens()
        self.assertTrue(gen.closed)
        self.assertTrue(gen.flag)

    def test_logging_deadlock_and_queue(self) -> None:
        broken = build("log_deadlock", "unfixed")
        schedule = Schedule(mode="free", seed=0, depth=1, n_max=4, k_budget=16)
        self.assertIsNone(search_first(broken, schedule, max_bound=0))
        hit = search_first(broken, schedule, max_bound=2)
        self.assertIsNotNone(hit)
        assert hit is not None
        # One switch after the emit takes the handler lock is enough.
        self.assertEqual(hit.bound, 1)
        self.assertEqual(hit.journal["failure"], "Deadlock")
        held = [(event["thread"], event["args"][0]) for event in hit.journal["events"]]
        self.assertEqual(held, [(1, "handler"), (2, "module")])

        fixed = build("log_queue", "fixed")
        for seed in range(20):
            trace = execute(fixed, seed=seed, depth=2, n_max=4, k_budget=16)
            self.assertEqual([item["message"] for item in trace.extra["delivered"]], ["tick-high"])
            self.assertEqual([item["message"] for item in trace.extra["skipped"]], ["tick-low"])
        self.assertIsNone(search_first(fixed, schedule, max_bound=2))

        every_level = scenario(
            "queue_all",
            {1: [("enq", "tick-low", 20), ("enq", "tick-high", 40)], 2: [("deq",), ("deq",)]},
            respect_handler_level=False,
            handler_level=30,
        )
        trace = execute(every_level, seed=0)
        self.assertEqual(len(trace.extra["delivered"]), 2)
        self.assertEqual(trace.extra["skipped"], [])

    def test_propagate_and_basicConfig(self) -> None:
        manager = Manager()
        handler = Handler("app")
        child = manager.getLogger("app.child")
        child.addHandler(handler)
        manager.root.addHandler(handler)
        manager.emit(child, 20, "twice")
        self.assertEqual(len(handler.records), 2)
        child.removeHandler(handler)
        manager.emit(child, 20, "once")
        self.assertEqual(len(handler.records), 3)
        self.assertEqual(handler.records[-1].message, "once")

        fresh = Manager()
        fresh.info("root", "hello")
        self.assertTrue(fresh.configured)
        self.assertEqual(fresh.lock_order[:2], ["module", "lastResort"])
        self.assertEqual(fresh.root.handlers[0].records[0].message, "hello")

    def test_fixture_parsers_reject_bad_shapes(self) -> None:
        port = load_port()
        self.assertEqual([task["id"] for task in port["tasks"]], ["scan-a", "scan-b"])
        levels = load_levels()
        self.assertEqual(levels["handler_level"], 30)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text(json.dumps({"tasks": [{"id": "", "cancelled": False}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_port(path)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_journal(path)
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_journal(path)
            path.write_text(json.dumps([1, 2]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_levels(path)

    def test_tampered_journal_is_rejected(self) -> None:
        saved = load_journal(REGRESSIONS / "cancel_unfixed.schedule.json")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cancel.schedule.json"
            dump_journal(path, saved)
            self.assertEqual(load_journal(path), saved)
            tampered = json.loads(json.dumps(saved))
            tampered["events"][2]["thread"] = 1
            dump_journal(path, tampered)
            with self.assertRaises(ValueError) as caught:
                load_journal(path)
            self.assertIn("trace_sha256", str(caught.exception))
            tampered = json.loads(json.dumps(saved))
            tampered["mode"] = "nogil"
            dump_journal(path, tampered)
            with self.assertRaises(ValueError):
                load_journal(path)

    def test_library_logger_is_quiet_until_configured(self) -> None:
        logger = logging.getLogger("yieldlab")
        self.assertTrue(any(isinstance(h, logging.NullHandler) for h in logger.handlers))
        with self.assertLogs("yieldlab", level="INFO") as captured:
            execute(build("ordering_d1", "fixed"), seed=0, n_max=4, k_budget=8)
            with self.assertRaises(Invariant):
                execute(build("ordering_d1", "unfixed"), policy="guide", guide=[2, 1, 3, 4], n_max=4)
        self.assertEqual(len(captured.records), 2)
        ok, failed = (record.getMessage() for record in captured.records)
        self.assertRegex(ok, r"^scenario=ordering_d1 variant=fixed failure=none trace=[0-9a-f]{64}$")
        self.assertRegex(failed, r"^scenario=ordering_d1 variant=unfixed failure=Invariant trace=[0-9a-f]{64}$")

    def test_cli_commands_and_errors(self) -> None:
        from yieldlab.cli import main

        # main() configures the root logger; put it back for the other tests.
        root = logging.getLogger()
        saved = (list(root.handlers), root.level)

        def restore() -> None:
            root.handlers[:] = saved[0]
            root.setLevel(saved[1])

        self.addCleanup(restore)

        def call(*argv: str) -> tuple:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main(list(argv))
                except SystemExit as exc:
                    code = exc.code
            return code, out.getvalue(), err.getvalue()

        code, out, _ = call("run", "ordering_d1", "--variant", "unfixed", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out),
            {"scenario": "ordering_d1", "variant": "unfixed", "threads": 4, "steps": 4, "mode": "free", "dry_run": True},
        )
        code, out, _ = call("list")
        self.assertEqual(out.split(), sorted(out.split()))
        self.assertIn("cancel_port", out.split())

        code, out, _ = call("search", "cancel_port", "--variant", "unfixed", "--bound", "2")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["bound"], 2)
        journal = str(REGRESSIONS / "cancel_unfixed.schedule.json")
        code, out, _ = call("replay", journal)
        self.assertEqual((code, json.loads(out)["failure"]), (1, "Deadlock"))
        code, out, _ = call("replay", journal, "--variant", "fixed")
        self.assertEqual((code, json.loads(out)["failure"], json.loads(out)["steps"]), (0, None, 8))

        code, out, _ = call("campaign", "ordering_d1", "--sample", "80")
        report = json.loads(out)
        self.assertEqual((report["sample"], report["floor"]), (80, 0.25))
        self.assertGreaterEqual(report["rate"], 1 / 8)

        for argv in (
            ("run", "log_queue"),
            ("run", "nope"),
            ("replay", str(PROJECT / "examples" / "port_tasks.json")),
            ("campaign", "lost_update", "--k-budget", "4"),
        ):
            code, out, err = call(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("yieldlab: error:", err)
            self.assertNotIn("Traceback", err)
        code, _, err = call("campaign", "ordering_d1", "--sample", "0")
        self.assertEqual(code, 2)
        self.assertIn("positive", err)


if __name__ == "__main__":
    unittest.main()
