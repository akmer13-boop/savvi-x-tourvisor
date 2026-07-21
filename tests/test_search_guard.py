import asyncio
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.budget import BudgetPolicy
from app.search_guard import (
    AttemptState,
    ClaimAction,
    SearchGuard,
    SearchGuardConfigurationError,
    SearchGuardStateError,
    SearchGuardUnavailable,
)


class MutableClock:
    def __init__(self, value: float = 1_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class SearchGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "guard.sqlite3"
        self.clock = MutableClock()
        self.guard = SearchGuard(
            self.db_path,
            "unit-test-hmac-secret-with-enough-bytes",
            namespace="test-tenant",
            clock=self.clock,
            window_ttl_seconds=72 * 60 * 60,
            replay_ttl_seconds=60,
            stale_attempt_seconds=300,
        )

    @staticmethod
    def search(**updates):
        payload = {
            "departure_city": "Москва",
            "country": "Турция",
            "resort": "Анталья",
            "date_from": "2026-08-10",
            "date_to": "2026-08-12",
            "nights_from": 7,
            "nights_to": 7,
            "adults": 2,
            "children": 0,
            "children_ages": [],
            "budget_type": "max",
            "budget_to": 500_000,
            "meal": "AI",
            "hotel_stars": 4,
            "image_mode": "links_in_text",
        }
        payload.update(updates)
        return payload

    def complete_success(self, claim, payload=None):
        self.assertEqual(claim.action, ClaimAction.CLAIMED)
        permit = self.guard.mark_dispatched(claim.attempt_id)
        self.guard.mark_succeeded(
            claim.attempt_id,
            payload or {"status": "ok", "request_id": claim.attempt_id},
        )
        return permit

    def test_first_second_and_third_actual_search(self):
        first = self.guard.claim("chat-1", self.search())
        self.assertEqual(first.dispatch_count, 0)
        first_permit = self.complete_success(first)
        self.assertEqual(first_permit.dispatch_number, 1)
        original_expiry = first_permit.window_expires_at

        self.clock.advance(10)
        second = self.guard.claim(
            "chat-1",
            self.search(country="Египет", resort="Хургада"),
        )
        second_permit = self.complete_success(second)
        self.assertEqual(second_permit.dispatch_number, 2)
        self.assertEqual(second_permit.window_expires_at, original_expiry)

        third = self.guard.claim(
            "chat-1",
            self.search(country="ОАЭ", resort="Дубай"),
        )
        self.assertEqual(third.action, ClaimAction.LIMIT_REACHED)
        self.assertEqual(third.dispatch_count, 2)
        self.assertEqual(third.remaining_dispatches, 0)

    def test_duplicate_replays_for_60_seconds_then_requires_explicit_refresh(self):
        first = self.guard.claim("chat-duplicate", self.search())
        self.complete_success(first, {"status": "ok", "price": 499_000})

        self.clock.advance(59)
        replay = self.guard.claim("chat-duplicate", self.search())
        self.assertEqual(replay.action, ClaimAction.REPLAY)
        self.assertEqual(replay.replay_payload["price"], 499_000)
        self.assertEqual(replay.dispatch_count, 1)

        self.clock.advance(2)
        stale = self.guard.claim("chat-duplicate", self.search())
        self.assertEqual(stale.action, ClaimAction.DUPLICATE)
        self.assertIsNone(stale.replay_payload)
        self.assertEqual(stale.dispatch_count, 1)

        self.guard.prune_expired()
        with sqlite3.connect(self.db_path) as connection:
            stored_payload = connection.execute(
                "SELECT replay_payload FROM search_attempts WHERE attempt_id = ?",
                (first.attempt_id,),
            ).fetchone()[0]
        self.assertIsNone(stored_payload)

    def test_explicit_refresh_is_second_fresh_search_but_not_a_third(self):
        first = self.guard.claim("chat-refresh", self.search())
        self.complete_success(first)

        refresh = self.guard.claim(
            "chat-refresh",
            self.search(),
            refresh_requested=True,
        )
        self.assertEqual(refresh.action, ClaimAction.CLAIMED)
        permit = self.complete_success(refresh)
        self.assertEqual(permit.dispatch_number, 2)

        third = self.guard.claim(
            "chat-refresh",
            self.search(),
            refresh_requested=True,
        )
        self.assertEqual(third.action, ClaimAction.LIMIT_REACHED)

    def test_preflight_clarification_does_not_consume_slot(self):
        claim = self.guard.claim("chat-clarification", self.search())
        self.guard.abandon_claim(claim.attempt_id)

        corrected = self.guard.claim(
            "chat-clarification",
            self.search(resort="Белек"),
        )
        permit = self.complete_success(corrected)
        self.assertEqual(permit.dispatch_number, 1)

    def test_preflight_failure_blocks_automatic_retry_without_using_slot(self):
        claim = self.guard.claim("chat-preflight-error", self.search())
        self.guard.mark_preflight_failed(claim.attempt_id, "DICTIONARY_TIMEOUT")

        duplicate = self.guard.claim("chat-preflight-error", self.search())
        self.assertEqual(duplicate.action, ClaimAction.DUPLICATE)
        self.assertEqual(duplicate.prior_state, AttemptState.FAILED)
        self.assertEqual(duplicate.dispatch_count, 0)

        changed = self.guard.claim(
            "chat-preflight-error",
            self.search(resort="Белек"),
        )
        permit = self.complete_success(changed)
        self.assertEqual(permit.dispatch_number, 1)

    def test_dispatched_failure_is_counted_and_not_automatically_retried(self):
        claim = self.guard.claim("chat-error", self.search())
        permit = self.guard.mark_dispatched(claim.attempt_id)
        self.assertEqual(permit.dispatch_number, 1)
        self.guard.mark_failed(claim.attempt_id, "UPSTREAM_TIMEOUT")

        duplicate = self.guard.claim("chat-error", self.search())
        self.assertEqual(duplicate.action, ClaimAction.DUPLICATE)
        self.assertEqual(duplicate.dispatch_count, 1)

        refresh = self.guard.claim(
            "chat-error",
            self.search(),
            refresh_requested=True,
        )
        self.assertEqual(refresh.action, ClaimAction.CLAIMED)

    def test_window_expires_72_hours_after_first_actual_dispatch(self):
        first = self.guard.claim("chat-ttl", self.search())
        first_permit = self.complete_success(first)

        self.clock.advance(24 * 60 * 60)
        self.assertEqual(
            self.guard.claim("chat-ttl", self.search()).action,
            ClaimAction.DUPLICATE,
        )

        self.clock.value = first_permit.window_expires_at
        fresh_window = self.guard.claim("chat-ttl", self.search())
        self.assertEqual(fresh_window.action, ClaimAction.CLAIMED)
        self.assertEqual(fresh_window.dispatch_count, 0)
        fresh_permit = self.guard.mark_dispatched(fresh_window.attempt_id)
        self.assertEqual(fresh_permit.dispatch_number, 1)
        self.assertEqual(fresh_permit.window_started_at, self.clock.value)

    def test_search_and_delivery_fingerprints_are_separate(self):
        first_request = self.search(
            image_mode="links_in_text",
            hotel_preferences="тихий отель",
        )
        claim = self.guard.claim("chat-delivery", first_request)
        self.complete_success(claim, {"status": "ok", "format": "links"})

        changed_delivery = self.search(
            image_mode="structured",
            hotel_preferences="рядом с центром",
        )
        duplicate = self.guard.claim("chat-delivery", changed_delivery)
        self.assertEqual(duplicate.search_fingerprint, claim.search_fingerprint)
        self.assertNotEqual(duplicate.delivery_fingerprint, claim.delivery_fingerprint)
        self.assertEqual(duplicate.action, ClaimAction.DUPLICATE)
        self.assertFalse(duplicate.delivery_matches)
        self.assertIsNone(duplicate.replay_payload)

    def test_replay_is_bound_to_server_policy_version(self):
        first = self.guard.claim(
            "chat-policy",
            self.search(),
            delivery_key={"whitelist_hash": "policy-a"},
        )
        self.complete_success(first, {"status": "ok"})

        changed_policy = self.guard.claim(
            "chat-policy",
            self.search(),
            delivery_key={"whitelist_hash": "policy-b"},
        )
        self.assertEqual(changed_policy.action, ClaimAction.DUPLICATE)
        self.assertFalse(changed_policy.delivery_matches)
        self.assertIsNone(changed_policy.replay_payload)

    def test_legacy_and_policy_budget_have_same_search_fingerprint(self):
        legacy = self.search()
        legacy.pop("budget_type")
        legacy.pop("budget_to")
        legacy["budget"] = 500_000
        policy = self.search(
            budget_type=None,
            budget_to=None,
            budget_policy={"mode": "MAX", "maximum": 500_000},
        )
        self.assertEqual(
            self.guard.search_fingerprint(legacy),
            self.guard.search_fingerprint(policy),
        )

        approx_request = self.search(
            budget_type="approx",
            budget_to=500_000,
        )
        approx_request["budget_policy"] = BudgetPolicy(
            budget_type="approx",
            price_from=450_000,
            price_to=550_000,
            anchor=500_000,
        )
        self.assertEqual(
            self.guard.search_fingerprint(self.search(budget_type="approx", budget_to=500_000)),
            self.guard.search_fingerprint(approx_request),
        )

        self.assertNotEqual(
            self.guard.search_fingerprint(self.search(budget_to=500_000)),
            self.guard.search_fingerprint(self.search(budget_to=600_000)),
        )

    def test_raw_chat_request_and_sensitive_replay_keys_are_not_persisted(self):
        raw_chat = "raw-chat-id-never-store"
        request = self.search(
            client_name="Sensitive Name",
            client_phone="+79990000000",
            auth_token="secret-token",
        )
        claim = self.guard.claim(raw_chat, request)
        self.complete_success(
            claim,
            {
                "status": "ok",
                "chat_id": raw_chat,
                "client_phone": "+79990000000",
                "unverified_preferences": ["sensitive free-form wish"],
                "nested": {"auth_token": "secret-token"},
            },
        )
        replay = self.guard.claim(raw_chat, request)
        self.assertEqual(replay.replay_payload, {"nested": {}, "status": "ok"})

        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT chat_key, search_fingerprint, delivery_fingerprint, replay_payload "
                "FROM search_attempts"
            ).fetchall()
        persisted = repr(rows)
        self.assertNotIn(raw_chat, persisted)
        self.assertNotIn("Sensitive Name", persisted)
        self.assertNotIn("+79990000000", persisted)
        self.assertNotIn("secret-token", persisted)
        self.assertNotIn("sensitive free-form wish", persisted)
        self.assertTrue(all(row[3] is None for row in rows))

    def test_concurrent_duplicate_creates_only_one_claim(self):
        workers = 8
        barrier = threading.Barrier(workers)

        def run_claim(_):
            barrier.wait()
            return self.guard.claim("chat-concurrent", self.search())

        with ThreadPoolExecutor(max_workers=workers) as executor:
            decisions = list(executor.map(run_claim, range(workers)))

        self.assertEqual(
            sum(decision.action is ClaimAction.CLAIMED for decision in decisions),
            1,
        )
        self.assertEqual(
            sum(decision.action is ClaimAction.IN_FLIGHT for decision in decisions),
            workers - 1,
        )

    def test_crashed_claim_is_released_and_crashed_dispatch_is_terminal(self):
        claimed = self.guard.claim("chat-stale-claim", self.search())
        self.clock.advance(301)
        replacement = self.guard.claim("chat-stale-claim", self.search())
        self.assertEqual(replacement.action, ClaimAction.CLAIMED)
        self.assertNotEqual(replacement.attempt_id, claimed.attempt_id)

        dispatched = self.guard.claim("chat-stale-dispatch", self.search())
        self.guard.mark_dispatched(dispatched.attempt_id)
        self.clock.advance(301)
        terminal = self.guard.claim("chat-stale-dispatch", self.search())
        self.assertEqual(terminal.action, ClaimAction.DUPLICATE)
        self.assertEqual(terminal.prior_state, AttemptState.FAILED)
        self.assertEqual(terminal.dispatch_count, 1)

    def test_async_facade_and_readiness(self):
        async def scenario():
            await self.guard.acheck_ready()
            claim = await self.guard.aclaim("chat-async", self.search())
            permit = await self.guard.amark_dispatched(claim.attempt_id)
            await self.guard.amark_succeeded(claim.attempt_id, {"status": "ok"})
            return permit

        permit = asyncio.run(scenario())
        self.assertEqual(permit.dispatch_number, 1)

    def test_prune_failure_makes_readiness_fail_closed_until_recovery(self):
        with patch.object(
            self.guard,
            "_transaction",
            side_effect=sqlite3.OperationalError("test-only failure"),
        ):
            with self.assertRaises(SearchGuardUnavailable):
                self.guard.prune_expired()

        with self.assertRaises(SearchGuardUnavailable):
            self.guard.check_ready()
        self.guard.prune_expired()
        self.guard.check_ready()

    def test_configuration_and_state_fail_closed(self):
        with self.assertRaises(SearchGuardConfigurationError):
            SearchGuard(":memory:", "long-enough-secret-for-test")
        with self.assertRaises(SearchGuardConfigurationError):
            SearchGuard(self.db_path, "short")
        with self.assertRaises(SearchGuardConfigurationError):
            SearchGuard(
                Path(self.tempdir.name) / "too-long-window.sqlite3",
                "long-enough-secret-for-test",
                window_ttl_seconds=72 * 60 * 60 + 1,
            )
        with self.assertRaises(SearchGuardConfigurationError):
            SearchGuard(
                Path(self.tempdir.name) / "too-long-replay.sqlite3",
                "long-enough-secret-for-test",
                replay_ttl_seconds=61,
            )
        with self.assertRaises(SearchGuardStateError):
            self.guard.mark_dispatched("not-an-attempt-id")


if __name__ == "__main__":
    unittest.main()
