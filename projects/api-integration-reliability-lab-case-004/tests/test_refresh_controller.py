"""Single-flight refresh, the pre-commit budget, and a dropped 200."""

from __future__ import annotations

import threading

from helpers import LabCase, refresh_request, token_posts
from lotcycle.errors import ReauthRequired, RefreshBudgetExhausted
from lotcycle.params import ACCESS_LIFETIME, PRECOMMIT_BUDGET
from lotcycle.tokens import token_hash


class RefreshControllerTest(LabCase):
    def test_dropped_success_is_not_resent(self) -> None:
        client = self.north()
        held = client.refresh_token
        self.lab.clock.advance(ACCESS_LIFETIME)
        self.lab.transport.drop_after_send = 1
        stamp = self.lab.clock.now()
        with self.assertRaises(ReauthRequired):
            client.ensure_access()
        self.assertEqual(self.lab.clock.now(), stamp)
        self.assertEqual(token_posts(self.lab), 1)
        self.assertEqual(len(self.lab.transport.attempts), 1)
        self.assertEqual(self.family()["active_generation"], 2)
        self.assertEqual(self.family()["status"], "active")
        self.assertEqual(client.refresh_token, held)
        self.assertEqual(client.status, "reauth_required")
        self.assertIn("refresh_post_send_stop", client.decisions)
        self.assertEqual(self.lab.store.reuse_count(client.family_id), 0)

        # The client must not present generation 1 again. A manual request does.
        replay = self.lab.auth.handle(refresh_request(client, refresh=held))
        self.assertEqual(replay.json()["error"], "invalid_grant")
        self.assertEqual(self.family()["status"], "reauth_required")
        self.assertIsNone(self.family()["active_generation"])
        self.assertEqual(self.lab.store.reuse_count(client.family_id), 1)
        self.assertEqual(self.lab.store.active_refresh_count(client.family_id), 0)
        self.assertIsNotNone(self.lab.store.find_refresh(token_hash(held))["consumed_at"])

    def test_two_callers_share_one_refresh(self) -> None:
        client = self.north()
        client.access_exp = self.lab.clock.now()
        found: list[str] = []
        entered = threading.Event()
        release = threading.Event()
        token_handler = self.lab.transport.handlers["token"]

        def parked_token_handler(request):
            # The leader is parked inside the exchange while the follower arrives.
            entered.set()
            release.wait(3)
            return token_handler(request)

        self.lab.transport.handlers["token"] = parked_token_handler

        def work() -> None:
            found.append(client.ensure_access())

        leader = threading.Thread(target=work, daemon=True)
        follower = threading.Thread(target=work, daemon=True)
        leader.start()
        self.assertTrue(entered.wait(3))
        follower.start()
        follower.join(0.2)
        # The follower is blocked on the refresh lock, not sending its own POST.
        self.assertTrue(follower.is_alive())
        self.assertEqual(len(self.lab.transport.attempts), 1)
        release.set()
        for thread in (leader, follower):
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0], found[1])
        self.assertEqual(token_posts(self.lab), 1)
        self.assertEqual(self.lab.store.reuse_count(client.family_id), 0)
        self.assertEqual(self.family()["active_generation"], 2)

    def test_single_flight_covers_the_precommit_retry(self) -> None:
        client = self.north()
        client.access_exp = self.lab.clock.now()
        self.lab.auth.fail_before_commit = 1
        found: list[str] = []

        def work() -> None:
            found.append(client.ensure_access())

        threads = [threading.Thread(target=work, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(found[0], found[1])
        self.assertEqual(token_posts(self.lab), 2)
        self.assertEqual(self.family()["active_generation"], 2)
        self.assertEqual(self.lab.store.reuse_count(client.family_id), 0)

    def test_precommit_500_is_retried_then_committed(self) -> None:
        client = self.north()
        self.lab.auth.fail_before_commit = 1
        self.lab.clock.advance(ACCESS_LIFETIME)
        client.ensure_access()
        self.assertEqual(token_posts(self.lab), 2)
        self.assertEqual(self.family()["active_generation"], 2)
        self.assertIn("refresh_precommit_retry", client.decisions)
        self.assertIn("refresh_ok", client.decisions)
        failed = [row for row in self.lab.store.logs if row["event"] == "token_rollback"]
        self.assertEqual(len(failed), 1)

    def test_precommit_budget_stops_at_three_sends(self) -> None:
        client = self.north()
        self.lab.auth.fail_before_commit = 5
        start = self.lab.clock.now()
        with self.assertRaises(RefreshBudgetExhausted):
            client.refresh()
        self.assertEqual(PRECOMMIT_BUDGET, 3)
        self.assertEqual(token_posts(self.lab), 3)
        self.assertEqual(len(self.lab.transport.sent), 3)
        self.assertEqual(self.family()["active_generation"], 1)
        self.assertEqual(self.family()["status"], "active")
        self.assertGreater(self.lab.clock.now(), start)
        self.assertEqual(self.lab.store.active_refresh_count(client.family_id), 1)

    def test_connection_closed_before_send_reuses_the_same_token(self) -> None:
        client = self.north()
        self.lab.transport.fail_before_send = 2
        client.refresh()
        self.assertEqual(len(self.lab.transport.attempts), 3)
        self.assertEqual(token_posts(self.lab), 1)
        self.assertEqual(self.family()["active_generation"], 2)
        self.assertEqual(client.decisions.count("refresh_presend_retry"), 2)
        self.assertIn("refresh_ok", client.decisions)

    def test_three_presend_failures_never_reach_the_server(self) -> None:
        client = self.north()
        self.lab.transport.fail_before_send = 3
        with self.assertRaises(RefreshBudgetExhausted):
            client.refresh()
        self.assertEqual(len(self.lab.transport.attempts), 3)
        self.assertEqual(token_posts(self.lab), 0)
        self.assertEqual(self.family()["active_generation"], 1)
        self.assertEqual(self.family()["status"], "active")
        self.assertEqual(client.status, "active")
