import asyncio
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import _prune_search_guard_periodically, app
from app.search_guard import SearchGuard


class ApiSearchGuardIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.guard = SearchGuard(
            Path(self.tempdir.name) / "guard.sqlite3",
            "integration-test-guard-secret-with-enough-bytes",
            namespace="api-integration-test",
        )
        self.client = TestClient(app)
        start = date.today() + timedelta(days=30)
        self.payload = {
            "departure_city": "Москва",
            "country": "Турция",
            "date_from": start.isoformat(),
            "date_to": (start + timedelta(days=6)).isoformat(),
            "nights_from": 7,
            "nights_to": 10,
            "adults": 2,
            "children": 0,
            "budget_type": "max",
            "budget_to": 500_000,
            "chat_id": "integration-chat",
            "image_mode": "none",
        }

    @staticmethod
    def _successful_no_matches_search() -> AsyncMock:
        async def search(request, *, before_dispatch=None):
            if before_dispatch is not None:
                await before_dispatch()
            return "integration-search-id", []

        return AsyncMock(side_effect=search)

    def test_replay_refresh_and_third_search_limit_are_enforced_at_api_boundary(self):
        search = self._successful_no_matches_search()
        with (
            patch("app.main.search_guard", self.guard),
            patch("app.main.TourvisorClient.search_tours", new=search),
        ):
            first = self.client.post("/tour-search", json=self.payload)
            replay = self.client.post("/tour-search", json=self.payload)
            refresh = self.client.post(
                "/tour-search",
                json={**self.payload, "refresh_requested": True},
            )
            blocked = self.client.post(
                "/tour-search",
                json={
                    **self.payload,
                    "budget_to": 600_000,
                    "refresh_requested": True,
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["reason"], "NO_MATCHES")
        self.assertFalse(first.json()["reused"])
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["reason"], "NO_MATCHES")
        self.assertTrue(replay.json()["reused"])
        self.assertEqual(refresh.status_code, 200)
        self.assertEqual(refresh.json()["reason"], "NO_MATCHES")
        self.assertFalse(refresh.json()["reused"])
        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(blocked.json()["reason"], "SEARCH_LIMIT_REACHED")
        self.assertEqual(search.await_count, 2)

    def test_guard_requires_chat_id_before_tourvisor(self):
        search = self._successful_no_matches_search()
        payload = {key: value for key, value in self.payload.items() if key != "chat_id"}
        with (
            patch("app.main.search_guard", self.guard),
            patch("app.main.TourvisorClient.search_tours", new=search),
        ):
            response = self.client.post("/tour-search", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reason"], "CHAT_ID_REQUIRED")
        search.assert_not_awaited()

    def test_numeric_chat_id_is_normalized_and_guarded(self):
        search = self._successful_no_matches_search()
        with (
            patch("app.main.search_guard", self.guard),
            patch("app.main.TourvisorClient.search_tours", new=search),
        ):
            first = self.client.post(
                "/tour-search",
                json={**self.payload, "chat_id": -10012345},
            )
            replay = self.client.post(
                "/tour-search",
                json={**self.payload, "chat_id": "-10012345"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["reused"])
        self.assertEqual(search.await_count, 1)

    def test_replay_restores_preferences_without_persisting_them(self):
        search = self._successful_no_matches_search()
        payload = {
            **self.payload,
            "hotel_preferences": "тихий отель",
            "beach_preferences": "песчаный пляж",
        }
        with (
            patch("app.main.search_guard", self.guard),
            patch("app.main.TourvisorClient.search_tours", new=search),
        ):
            first = self.client.post("/tour-search", json=payload)
            replay = self.client.post("/tour-search", json=payload)

        expected = ["тихий отель", "песчаный пляж"]
        self.assertEqual(first.json()["unverified_preferences"], expected)
        self.assertEqual(replay.json()["unverified_preferences"], expected)
        self.assertTrue(replay.json()["reused"])

    def test_clarification_does_not_consume_a_search(self):
        search = self._successful_no_matches_search()
        incomplete = {
            **self.payload,
            "children": 1,
            "children_ages": [],
        }
        corrected = {
            **self.payload,
            "children": 1,
            "children_ages": [7],
        }
        with (
            patch("app.main.search_guard", self.guard),
            patch("app.main.TourvisorClient.search_tours", new=search),
        ):
            clarification = self.client.post("/tour-search", json=incomplete)
            first_search = self.client.post("/tour-search", json=corrected)

        self.assertEqual(clarification.status_code, 200)
        self.assertEqual(clarification.json()["status"], "needs_clarification")
        self.assertEqual(clarification.json()["reason"], "CHILD_AGES_REQUIRED")
        self.assertEqual(first_search.status_code, 200)
        self.assertEqual(first_search.json()["reason"], "NO_MATCHES")
        self.assertEqual(search.await_count, 1)

    def test_background_retention_cleanup_runs(self):
        guard = AsyncMock()

        async def scenario():
            sleeps = AsyncMock(side_effect=[None, asyncio.CancelledError()])
            with patch("app.main.asyncio.sleep", new=sleeps):
                with self.assertRaises(asyncio.CancelledError):
                    await _prune_search_guard_periodically(guard)

        asyncio.run(scenario())
        guard.aprune_expired.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
