import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


class ApiContractTest(unittest.TestCase):
    def setUp(self):
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
            "budget": 500,
            "image_mode": "none",
        }

    def test_short_response_contains_structured_routing_fields(self):
        response = self.client.post(
            "/tour-search",
            json=self.payload,
            headers={"X-Request-ID": "test-request-1"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["request_id"], "test-request-1")
        self.assertEqual(response.headers["X-Request-ID"], "test-request-1")
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["reason"], "FOUND")
        self.assertIn("whitelist_version", body)

    def test_children_without_ages_request_clarification(self):
        payload = {**self.payload, "children": 1, "children_ages": []}
        response = self.client.post("/tour-search", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reason"], "CHILD_AGES_REQUIRED")
        self.assertFalse(response.json()["found"])

    def test_invalid_schema_has_structured_422(self):
        response = self.client.post("/tour-search", json={"country": "Турция"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["reason"], "INVALID_REQUEST")
        self.assertIn("request_id", response.json())

    def test_debug_endpoint_is_disabled(self):
        response = self.client.get("/suvvy-debug")
        self.assertEqual(response.status_code, 404)

    def test_free_text_preferences_are_preserved_for_manager_verification(self):
        payload = {
            **self.payload,
            "hotel_preferences": "тихий семейный отель",
            "beach_preferences": "первая линия",
        }
        response = self.client.post("/tour-search", json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["unverified_preferences"],
            ["тихий семейный отель", "первая линия"],
        )
        self.assertIn("переданы менеджеру", body["client_text"])

    def test_bearer_authentication(self):
        with patch.object(settings, "suvvy_webhook_token", "test-only-token"):
            denied = self.client.post("/tour-search", json=self.payload)
            allowed = self.client.post(
                "/tour-search",
                json=self.payload,
                headers={"Authorization": "Bearer test-only-token"},
            )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)

    def test_upstream_timeout_has_structured_reason(self):
        timeout = httpx.ReadTimeout("test timeout")
        with patch(
            "app.main.TourvisorClient.search_tours",
            new=AsyncMock(side_effect=timeout),
        ):
            response = self.client.post("/tour-search", json=self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["reason"], "UPSTREAM_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
