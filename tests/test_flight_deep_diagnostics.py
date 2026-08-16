import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.config import settings
from app.models import TourOption
from app.operator_policy import OperatorEntry, OperatorPolicy
from app.tourvisor_client import TourvisorClient


class TourvisorFlightDeepDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.policy = OperatorPolicy(
            version="test-v1",
            entries=(OperatorEntry(13, "Anex", "active_contract"),),
            sha256="f" * 64,
            enforced=True,
        )

    @staticmethod
    def flight_payload() -> dict:
        return {
            "error": {"code": 0, "reason": ""},
            "flights": [
                {
                    "isDefault": True,
                    "dateForward": "2026-11-11",
                    "dateBackward": "2026-11-18",
                    "price": {"currency": "RUB", "value": 479_425},
                    "forward": [
                        {
                            "departure": {
                                "date": "2026-11-11",
                                "time": "05:40",
                                "port": {"name": "Москва", "shortName": "VKO"},
                            },
                            "arrival": {
                                "date": "2026-11-11",
                                "time": "10:15",
                                "port": {"name": "Анталья", "shortName": "AYT"},
                            },
                            "noPlaces": False,
                        }
                    ],
                    "backward": [
                        {
                            "departure": {
                                "date": "2026-11-18",
                                "time": "12:30",
                                "port": {"name": "Анталья", "shortName": "AYT"},
                            },
                            "arrival": {
                                "date": "2026-11-18",
                                "time": "17:05",
                                "port": {"name": "Москва", "shortName": "VKO"},
                            },
                            "noPlaces": False,
                        }
                    ],
                }
            ],
            "info": {"flags": {"noFlight": False}},
        }

    @staticmethod
    def tours(count: int = 3) -> list[TourOption]:
        return [
            TourOption(
                country="Турция",
                hotel=f"Hotel {index}",
                departure_city="Москва",
                price=470_000,
                currency="RUB",
                tour_id=str(index),
            )
            for index in range(1, count + 1)
        ]

    def test_diagnostic_mode_hard_caps_billable_call_and_runs_preflight(self):
        client = TourvisorClient(policy=self.policy)
        client._get = AsyncMock(return_value={"id": "1"})
        client._get_flight_payload = AsyncMock(return_value=self.flight_payload())
        tours = self.tours(3)

        with (
            patch.object(settings, "mock_tourvisor", False),
            patch.object(settings, "tourvisor_enable_flight_actualization", True),
            patch.object(settings, "tourvisor_flight_actualization_limit", 3),
            patch.object(settings, "tourvisor_flight_actualization_concurrency", 3),
            patch.object(settings, "tourvisor_flight_diagnostic_mode", True),
            patch.object(settings, "tourvisor_flight_diagnostic_connect_timeout_seconds", 10),
            patch.object(settings, "tourvisor_flight_diagnostic_read_timeout_seconds", 90),
        ):
            asyncio.run(client.enrich_tours_with_flight_details(tours))

        client._get.assert_awaited_once()
        self.assertIn("/tours/1", client._get.await_args.args[1])
        self.assertNotIn("/flights", client._get.await_args.args[1])
        self.assertEqual(client._get.await_args.kwargs["params"], {"currency": "RUB"})
        client._get_flight_payload.assert_awaited_once()
        call = client._get_flight_payload.await_args
        self.assertEqual(call.kwargs["tour_id"], "1")
        self.assertTrue(call.kwargs["diagnostic_mode"])
        self.assertEqual(call.kwargs["connect_timeout_seconds"], 10)
        self.assertEqual(call.kwargs["read_timeout_seconds"], 90)
        self.assertTrue(tours[0].flight_actualized)
        self.assertFalse(tours[1].flight_actualized)
        self.assertFalse(tours[2].flight_actualized)

    def test_diagnostic_preflight_failure_skips_billable_flights_call(self):
        client = TourvisorClient(policy=self.policy)
        client._get = AsyncMock(side_effect=RuntimeError("tour preflight failed"))
        client._get_flight_payload = AsyncMock(return_value=self.flight_payload())
        tour = self.tours(1)[0]

        with (
            patch.object(settings, "mock_tourvisor", False),
            patch.object(settings, "tourvisor_enable_flight_actualization", True),
            patch.object(settings, "tourvisor_flight_actualization_limit", 1),
            patch.object(settings, "tourvisor_flight_actualization_concurrency", 1),
            patch.object(settings, "tourvisor_flight_diagnostic_mode", True),
            self.assertLogs("app.tourvisor_client", level="WARNING") as captured,
        ):
            asyncio.run(client.enrich_tours_with_flight_details([tour]))

        client._get_flight_payload.assert_not_awaited()
        self.assertFalse(tour.flight_actualized)
        self.assertEqual(tour.price, 470_000)
        self.assertIn("TOURVISOR_TOUR_PREFLIGHT_FAILED", "\n".join(captured.output))

    def test_diagnostic_stream_logs_safe_response_headers(self):
        client = TourvisorClient(policy=self.policy)

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(str(request.url).endswith("/search/api/v1/tours/1/flights?currency=RUB"))
            return httpx.Response(
                200,
                json=self.flight_payload(),
                headers={"content-type": "application/json"},
            )

        async def run_probe():
            timeout = httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                timeout=timeout,
            ) as http_client:
                with self.assertLogs("app.tourvisor_client", level="INFO") as captured:
                    payload = await client._get_flight_payload(
                        http_client,
                        tour_id="1",
                        currency="RUB",
                        diagnostic_mode=True,
                        connect_timeout_seconds=10,
                        read_timeout_seconds=90,
                    )
            return payload, captured.output

        payload, output = asyncio.run(run_probe())
        self.assertEqual(payload, self.flight_payload())
        joined = "\n".join(output)
        self.assertIn("TOURVISOR_FLIGHT_RESPONSE_HEADERS", joined)
        self.assertIn("status=200", joined)
        self.assertIn("content_type=application/json", joined)
        self.assertIn("TOURVISOR_FLIGHT_REQUEST_SUCCESS", joined)

    def test_effective_read_timeout_switches_only_in_diagnostic_mode(self):
        with (
            patch.object(settings, "tourvisor_flight_timeout_seconds", 45),
            patch.object(settings, "tourvisor_flight_diagnostic_read_timeout_seconds", 90),
            patch.object(settings, "tourvisor_flight_diagnostic_mode", False),
        ):
            self.assertEqual(settings.effective_flight_read_timeout_seconds, 45)

        with (
            patch.object(settings, "tourvisor_flight_timeout_seconds", 45),
            patch.object(settings, "tourvisor_flight_diagnostic_read_timeout_seconds", 90),
            patch.object(settings, "tourvisor_flight_diagnostic_mode", True),
        ):
            self.assertEqual(settings.effective_flight_read_timeout_seconds, 90)


if __name__ == "__main__":
    unittest.main()
