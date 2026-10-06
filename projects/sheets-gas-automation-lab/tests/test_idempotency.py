"""Operation-id ledger versus append and content-hash baselines."""

from __future__ import annotations

import unittest
from decimal import Decimal

import support  # noqa: F401

from sessionfee.canonical import SessionInput, fingerprint, user_entered_stub
from sessionfee.coercion import store_value
from sessionfee.idempotency import AppendBaseline, ContentHashBaseline, Ledger
from sessionfee.retry import retry_exchange


def _session(operation_id: str, hours: str = "2.5") -> SessionInput:
    return SessionInput(operation_id, "2026-10-07", "DESK-NORTH", Decimal(hours), Decimal("4000"))


class IdempotencyTests(unittest.TestCase):
    def test_same_key_same_payload_one_row_and_append_baseline_two_rows(self) -> None:
        session = _session("op-1")
        canonical = session.canonical()
        row = {"operation_id": "op-1"}
        baseline = AppendBaseline()
        baseline.exchange("op-1", canonical, row)
        baseline.exchange("op-1", canonical, row)
        ledger = Ledger()
        first = ledger.exchange("op-1", canonical, row)
        second = ledger.exchange("op-1", canonical, row)
        self.assertEqual(len(baseline.rows), 2)
        self.assertEqual(len(ledger.rows), 1)
        self.assertEqual(first.name, "applied")
        self.assertEqual(second.name, "replay")
        self.assertEqual(second.result, first.result)
        self.assertEqual(second.http_comment, 200)

    def test_same_key_different_payload_conflicts_and_does_not_write(self) -> None:
        ledger = Ledger()
        original = _session("op-1", "2.5").canonical()
        changed = _session("op-1", "3").canonical()
        ledger.exchange("op-1", original, {"n": 1})
        conflict = ledger.exchange("op-1", changed, {"n": 2})
        self.assertEqual(conflict.name, "payload_conflict")
        self.assertEqual(conflict.http_comment, 422)
        self.assertEqual(len(ledger.rows), 1)
        self.assertEqual(ledger.rows[0], {"n": 1})

    def test_in_progress_conflict_does_not_write_until_completion_then_replays(self) -> None:
        ledger = Ledger()
        canonical = _session("op-1").canonical()
        started = ledger.open_attempt("op-1", canonical)
        overlap = ledger.open_attempt("op-1", canonical)
        self.assertEqual(started.name, "started")
        self.assertEqual(overlap.name, "in_progress_conflict")
        self.assertEqual(overlap.http_comment, 409)
        self.assertEqual(ledger.rows, [])
        ledger.finish_attempt("op-1", {"stored": True}, {"stored": True})
        replay = ledger.open_attempt("op-1", canonical)
        self.assertEqual(replay.name, "replay")
        self.assertEqual(len(ledger.rows), 1)

    def test_missing_key_writes_nothing(self) -> None:
        ledger = Ledger()
        outcome = ledger.exchange("  ", _session("ignored").canonical(), {"n": 1})
        self.assertEqual(outcome.name, "missing_key")
        self.assertEqual(outcome.http_comment, 400)
        self.assertEqual(ledger.rows, [])
        self.assertEqual(ledger.exchange(None, _session("ignored").canonical(), {"n": 1}).name, "missing_key")
        self.assertEqual(ledger.rows, [])

    def test_two_keys_same_payload_two_rows_and_content_hash_drops_the_second(self) -> None:
        left = _session("op-left", "1")
        right = _session("op-right", "1")
        self.assertNotEqual(left.canonical()["operation_id"], right.canonical()["operation_id"])
        self.assertEqual(left.canonical()["hours"], right.canonical()["hours"])
        content = ContentHashBaseline()
        content.exchange(left.operation_id, left.canonical(), {"id": "left"})
        dropped = content.exchange(right.operation_id, right.canonical(), {"id": "right"})
        ledger = Ledger()
        ledger.exchange(left.operation_id, left.canonical(), {"id": "left"})
        kept = ledger.exchange(right.operation_id, right.canonical(), {"id": "right"})
        self.assertEqual(dropped.name, "dropped")
        self.assertEqual(len(content.rows), 1)
        self.assertEqual(kept.name, "applied")
        self.assertEqual(len(ledger.rows), 2)

    def test_decimal_text_and_pre_coercion_fingerprint_are_stable(self) -> None:
        wide = _session("op-1", "2.50")
        narrow = _session("op-1", "2.5")
        self.assertEqual(fingerprint(wide.canonical()), fingerprint(narrow.canonical()))
        again = fingerprint(narrow.canonical())
        self.assertEqual(again, fingerprint(narrow.canonical()))
        record = narrow.canonical()
        reordered = {key: record[key] for key in reversed(list(record))}
        reordered["rate"] = dict(reversed(list(record["rate"].items())))
        self.assertNotEqual(list(reordered), list(record))
        self.assertEqual(fingerprint(reordered), fingerprint(record))

    def test_user_entered_stub_changes_fingerprint_only_if_hashed_after(self) -> None:
        canonical = _session("op-1").canonical()
        stubbed = user_entered_stub(canonical)
        self.assertEqual(canonical["session_date"], "2026-10-07")
        self.assertNotEqual(stubbed["session_date"], canonical["session_date"])
        self.assertNotEqual(fingerprint(canonical), fingerprint(stubbed))
        ledger = Ledger()
        first = ledger.exchange("op-1", canonical, {"n": 1})
        second = ledger.exchange("op-1", canonical, {"n": 1})
        self.assertEqual(second.name, "replay")
        self.assertEqual(len(ledger.rows), 1)
        self.assertEqual(first.fingerprint, fingerprint(canonical))
        self.assertNotEqual(first.fingerprint, fingerprint(stubbed))

    def test_retry_backoff_replays_after_in_progress(self) -> None:
        ledger = Ledger()
        canonical = _session("op-1").canonical()
        ledger.open_attempt("op-1", canonical)
        delays: list[int] = []

        def sleep(delay: int) -> None:
            delays.append(delay)
            if delay == 50:
                ledger.finish_attempt("op-1", {"stored": True}, {"stored": True})

        outcome, used = retry_exchange(
            ledger,
            "op-1",
            canonical,
            {"stored": True},
            (0, 50, 100),
            sleep,
        )
        self.assertEqual(used, [50])
        self.assertEqual(delays, [50])
        self.assertEqual(outcome.name, "replay")
        self.assertEqual(len(ledger.rows), 1)

    def test_raw_keeps_identifier_text_and_user_entered_stub_parses_it(self) -> None:
        raw = store_value("00123", "RAW")
        parsed = store_value("00123", "USER_ENTERED")
        formula_raw = store_value("=SUM(F2:F4)", "RAW")
        formula_ui = store_value("=SUM(F2:F4)", "USER_ENTERED")
        date_ui = store_value("2026-10-07", "USER_ENTERED")
        self.assertEqual(raw, {"input": "RAW", "kind": "text", "value": "00123"})
        self.assertEqual(parsed["kind"], "number")
        self.assertEqual(parsed["value"], "123")
        self.assertEqual(formula_raw["kind"], "text")
        self.assertEqual(formula_ui["kind"], "formula")
        self.assertEqual(date_ui["kind"], "serial")
        self.assertNotEqual(date_ui["value"], "2026-10-07")


if __name__ == "__main__":
    unittest.main()
