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
            headers={"X-Request-ID": "0123456789abcdef0123456789abcdef"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["request_id"], "0123456789abcdef0123456789abcdef")
        self.assertEqual(
            response.headers["X-Request-ID"],
            "0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["reason"], "FOUND")
        self.assertIn("whitelist_version", body)

    def test_non_uuid_request_id_is_replaced_before_logging(self):
        response = self.client.post(
            "/tour-search",
            json=self.payload,
            headers={"X-Request-ID": "suvvy-123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["request_id"], "suvvy-123")
        self.assertRegex(response.json()["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(
            response.headers["X-Request-ID"],
            response.json()["request_id"],
        )

    def test_children_without_ages_request_clarification(self):
        payload = {**self.payload, "children": 1, "children_ages": []}
        response = self.client.post("/tour-search", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reason"], "CHILD_AGES_REQUIRED")
        self.assertFalse(response.json()["found"])

    def test_correctable_schema_error_is_structured_http_200(self):
        response = self.client.post("/tour-search", json={"country": "Турция"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "needs_clarification")
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

    def test_transitional_body_token_is_authenticated_before_schema(self):
        with (
            patch.object(settings, "suvvy_webhook_token", "test-only-token"),
            patch.object(settings, "suvvy_allow_body_token", True),
        ):
            allowed = self.client.post(
                "/tour-search",
                json={**self.payload, "auth_token": "test-only-token"},
            )
            unauthenticated_schema_error = self.client.post(
                "/tour-search",
                json={"country": "Турция"},
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(unauthenticated_schema_error.status_code, 401)
        self.assertEqual(unauthenticated_schema_error.json()["reason"], "UNAUTHORIZED")

    def test_previous_bearer_is_accepted_during_rotation_overlap(self):
        with (
            patch.object(settings, "suvvy_webhook_token", "test-only-current"),
            patch.object(
                settings,
                "suvvy_previous_webhook_token",
                "test-only-previous",
            ),
            patch.object(settings, "suvvy_allow_body_token", False),
        ):
            response = self.client.post(
                "/tour-search",
                json=self.payload,
                headers={"Authorization": "Bearer test-only-previous"},
            )
        self.assertEqual(response.status_code, 200)

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
        self.assertEqual(
            response.json()["client_text"],
            "Сейчас не удалось получить подборку. Я зафиксировала Ваш запрос — "
            "менеджер свяжется с Вами в ближайшее время.",
        )

    def test_china_and_four_children_route_without_tourvisor(self):
        for payload, reason in (
            ({**self.payload, "country": "China"}, "DESTINATION_BLOCKED"),
            (
                {
                    **self.payload,
                    "children": 4,
                    "children_ages": [2, 5, 8, 12],
                },
                "MANAGER_REQUIRED_TOO_MANY_CHILDREN",
            ),
        ):
            with self.subTest(reason=reason):
                with patch(
                    "app.main.TourvisorClient.search_tours",
                    new=AsyncMock(),
                ) as search:
                    response = self.client.post("/tour-search", json=payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "error")
                self.assertEqual(response.json()["reason"], reason)
                search.assert_not_awaited()

    def test_client_operator_ids_are_rejected_without_search(self):
        with patch(
            "app.main.TourvisorClient.search_tours",
            new=AsyncMock(),
        ) as search:
            response = self.client.post(
                "/tour-search",
                json={**self.payload, "operatorIds": [36]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reason"], "FORBIDDEN_OPERATOR_FILTER")
        search.assert_not_awaited()

    def test_bearer_is_checked_before_schema_when_body_token_is_disabled(self):
        with (
            patch.object(settings, "suvvy_webhook_token", "test-only-token"),
            patch.object(settings, "suvvy_allow_body_token", False),
        ):
            response = self.client.post("/tour-search", json={"country": "Турция"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["reason"], "UNAUTHORIZED")


if __name__ == "__main__":
    unittest.main()
