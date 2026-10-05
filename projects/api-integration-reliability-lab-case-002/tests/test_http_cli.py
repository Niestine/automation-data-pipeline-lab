"""HTTP surface, redacted logs, example fixtures, and the CLI."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import helpers
from quay_inbox.__main__ import main
from quay_inbox.coverage import sign_coverage
from quay_inbox.horizons import BODY_CAP_BYTES
from quay_inbox.httpmsg import HttpRequest
from quay_inbox.lab import project_root, run_demo
from quay_inbox.mac import github_style_body_mac
from quay_inbox.receiver import ADMIN_PATH, UNAUTHORIZED, Receiver
from quay_inbox.schedule import full_jitter_delay
from quay_inbox.secrets import ROUTE_A, Route
from quay_inbox.store import Store


class HttpSurfaceTests(unittest.TestCase):
    def _receiver(self, admin_replay=None):
        store, clock = helpers.make_store_clock()
        routes = [
            helpers.standard_route(),
            Route("coverage", "/hooks/coverage", "coverage", secrets=(ROUTE_A,), keyid="quay-coverage"),
        ]
        receiver = Receiver(store, clock, routes, csrf_token="csrf-lab-token", admin_replay=admin_replay)
        return store, clock, receiver

    def test_status_matrix_and_redacted_log(self) -> None:
        replayed: list[list[str]] = []
        store, clock, receiver = self._receiver(admin_replay=replayed.append)
        get_response = receiver.handle(HttpRequest("GET", "/hooks/quay", "hooks.quay.example", {}, b""))
        self.assertEqual(get_response.status, 405)
        self.assertEqual(get_response.headers.get("allow"), "POST")
        oversized = HttpRequest("POST", "/hooks/quay", "hooks.quay.example", {}, b"x" * (BODY_CAP_BYTES + 1))
        self.assertEqual(receiver.handle(oversized).status, 413)
        exact = HttpRequest("POST", "/hooks/quay", "hooks.quay.example", {}, b"{" * BODY_CAP_BYTES)
        self.assertEqual(receiver.handle(exact).status, 401)
        self.assertEqual(receiver.auth_failures, 1)

        replay_body = b'{"event_ids":["msg_quay_0001"]}'
        admin = receiver.handle(HttpRequest("POST", ADMIN_PATH, "hooks.quay.example", {}, replay_body))
        self.assertEqual(admin.status, 403)
        self.assertEqual(replayed, [])
        token = {"x-csrf-token": "csrf-lab-token"}
        empty = receiver.handle(HttpRequest("POST", ADMIN_PATH, "hooks.quay.example", token, b"{}"))
        self.assertEqual(empty.status, 400)
        allowed = receiver.handle(HttpRequest("POST", ADMIN_PATH, "hooks.quay.example", token, replay_body))
        self.assertEqual(allowed.status, 202)
        self.assertEqual(allowed.json(), {"replayed": 1})
        self.assertEqual(replayed, [["msg_quay_0001"]])
        _store, _clock, unwired = self._receiver()
        missing_route = unwired.handle(HttpRequest("POST", ADMIN_PATH, "hooks.quay.example", token, replay_body))
        self.assertEqual(missing_route.status, 404)

        body = helpers.standard_body()
        bad = helpers.signed_request(body, timestamp=clock.time())
        bad = HttpRequest(
            bad.method,
            bad.path,
            bad.authority,
            {**bad.headers, "webhook-signature": "v1,not-a-signature"},
            bad.body,
        )
        denied = receiver.handle(bad)
        self.assertEqual(denied.status, 401)
        self.assertEqual(denied.body, UNAUTHORIZED.body)
        text = denied.body.decode("utf-8")
        self.assertNotIn("timestamp", text)
        self.assertNotIn("signature", text)
        self.assertNotIn("quay-lab-route-secret", text)
        log_blob = json.dumps(store.logs)
        self.assertIn("msg_quay_0001", log_blob)
        self.assertNotIn("v1,not-a-signature", log_blob)
        self.assertNotIn(ROUTE_A.decode("ascii"), log_blob)
        self.assertNotIn("webhook-signature", log_blob)

        malformed = helpers.signed_request(b"{", timestamp=clock.time(), event_id="msg_bad_json")
        self.assertEqual(receiver.handle(malformed).status, 400)
        unsigned_malformed = HttpRequest(
            "POST",
            "/hooks/quay",
            "hooks.quay.example",
            {"webhook-id": "msg_bad_json", "webhook-timestamp": str(clock.time()), "webhook-signature": "v1,nope"},
            b"{",
        )
        self.assertEqual(receiver.handle(unsigned_malformed).status, 401)

        github = HttpRequest(
            "POST",
            "/hooks/quay",
            "hooks.quay.example",
            {
                "x-github-delivery": "delivery-2",
                "x-hub-signature-256": github_style_body_mac(ROUTE_A, body),
            },
            body,
        )
        self.assertEqual(receiver.handle(github).status, 401)

        good = receiver.handle(helpers.signed_request(body, timestamp=clock.time()))
        self.assertEqual(good.status, 200)
        self.assertNotIn("x-csrf-token", good.headers)

        coverage_body = b'{"api_version":"2024-09-01","data":{"berth":"Q3","id":"rel_c","status":"granted"},"id":"msg_cov_1","type":"release.granted"}'
        covered = sign_coverage(
            ROUTE_A,
            method="POST",
            authority="hooks.quay.example",
            path="/hooks/coverage",
            body=coverage_body,
        )
        accepted = receiver.handle(
            HttpRequest("POST", "/hooks/coverage", "hooks.quay.example", covered, coverage_body)
        )
        self.assertEqual(accepted.status, 200)
        self.assertIn("msg_cov_1", store.inbox)

    def test_examples_and_cli(self) -> None:
        report = run_demo(helpers.EXAMPLES, seed=7)
        self.assertEqual(report["accepted"], 2)
        self.assertEqual(report["effects"], 3)
        self.assertEqual(report["inbox"], 3)
        self.assertEqual(report["acks"], 1)
        self.assertEqual(report["gate_effects"], 3)
        self.assertEqual(report["resolver_lookups"], 1)
        self.assertEqual(report["pinned_address"], "203.0.113.10")
        self.assertEqual(report["host"], "hooks.quay.example")
        self.assertEqual(report["sni"], "hooks.quay.example")
        self.assertEqual(report["connections"], 3)
        expected_delay = full_jitter_delay(0, random.Random(7), base=8, cap=64)
        self.assertEqual(report["throttle_delays"], [expected_delay])
        blob = json.dumps(report)
        self.assertNotIn("whsec_", blob)
        self.assertNotIn(ROUTE_A.decode("ascii"), blob)

        dry = run_demo(helpers.EXAMPLES, dry_run=True, seed=7)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["inbox"], 0)
        self.assertEqual(dry["effects"], 0)
        self.assertEqual(dry["acks"], 0)
        self.assertEqual(dry["accepted"], 0)

        buffer = StringIO()
        with redirect_stdout(buffer):
            code = main(["--examples", str(helpers.EXAMPLES), "--seed", "7"])
        self.assertEqual(code, 0)
        printed = json.loads(buffer.getvalue())
        self.assertEqual(printed["effects"], 3)
        self.assertEqual(printed["throttle_delays"], [expected_delay])

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "inbox.json"
            with redirect_stdout(StringIO()):
                code = main(["--examples", str(helpers.EXAMPLES), "--state", str(state), "--seed", "7"])
            self.assertEqual(code, 0)
            self.assertTrue(state.exists())
            loaded = Store()
            loaded.load(state)
            self.assertEqual(loaded.memo_misses, 3)
            checkpoint = json.loads(Path(tmp, "inbox.checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["last_id"], "msg_quay_0003")
            self.assertIsNotNone(checkpoint["watermark_created"])

        error = StringIO()
        with redirect_stderr(error):
            missing = main(["--examples", str(project_root() / "missing-examples")])
        self.assertEqual(missing, 2)
        self.assertNotIn("whsec_", error.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            for name in ("notices.json", "fault_script.json"):
                Path(tmp, name).write_bytes((helpers.EXAMPLES / name).read_bytes())
            Path(tmp, "routes.json").write_text('{"quay": {"key_text": "too-short"}}', encoding="utf-8")
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["--examples", tmp]), 2)


if __name__ == "__main__":
    unittest.main()
